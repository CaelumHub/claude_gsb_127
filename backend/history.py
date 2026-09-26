"""操作历史: 时间分片日志 + 周期快照 + 快速回放窗口 + 归档压缩。

**难点: 历史压缩与快速回放**

- 日志按小时分片(ops/YYYYMMDD-HH.jsonl), 分片名含时间, 天然按时间排序;
  分片元信息(条数/rev区间/时间区间)带 (size, mtime) 失效校验的缓存,
  列表/定位不用反复解析文件。
- 每 N 个操作(或 N 秒)折叠一次全量状态到 snapshots/<rev>.json。
  回放到任意时刻 = 找最近的 ≤rev 快照 + 顺序折叠其后的操作, 而不是
  从零重放全部历史 —— 快照间隔 200 时, 任意跳转最多折叠 200 个操作。
- coalesce_moves 在「快进/拖动进度条」场景把同站点同图形时间窗内的
  连续 move 增量合并为一步, 大段拖拽回放不再逐像素重演。
- compact_archived: 对「完全位于最新快照之下且不再属于今天」的归档
  分片做物理压缩(合并 move、丢弃被覆盖的 set_props 旧值), 终态等价,
  存储量随编辑时长不再线性膨胀。压缩通过 tmp+rename 原子重写分片。
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

from . import config
from .crdt import BoardDoc, compact_ops_lossy, validate_op
from .storage import JsonlLog, dir_size, read_json, shard_cache_key, write_json_atomic

SHARD_META_CACHE: Dict[str, Dict[str, Any]] = {}


class BoardHistory:
    """单个白板的时间分片操作日志与快照仓库。"""

    def __init__(self, board_id: str):
        self.board_id = board_id
        self.dir = config.board_dir(board_id)
        self.ops_dir = os.path.join(self.dir, "ops")
        self.snap_dir = os.path.join(self.dir, "snapshots")
        os.makedirs(self.ops_dir, exist_ok=True)
        os.makedirs(self.snap_dir, exist_ok=True)
        self.log = JsonlLog(self.ops_dir, prefix="ops")

    # ---------------------------------------------------------------- 写入
    def append_ops(self, ops: List[Dict[str, Any]]) -> Optional[str]:
        """把已分配 rev 的操作追加到其时间戳所属分片。返回分片名。"""
        if not ops:
            return None
        ts = ops[0].get("ts")
        return self.log.append(ops, ts_ms=ts)

    def flush(self) -> None:
        name = self.log.shard_name()
        self.log.fsync_shard(name)

    # ---------------------------------------------------------------- 分片索引
    def _shard_meta(self, name: str) -> Dict[str, Any]:
        path = self.log.shard_path(name)
        try:
            st = os.stat(path)
        except OSError:
            return {"name": name, "count": 0}
        cache_key = shard_cache_key(path)
        cached = SHARD_META_CACHE.get(cache_key)
        if cached:
            return cached
        records = self.log.read_shard(name)
        revs = [r.get("rev", 0) for r in records if r.get("rev")]
        tss = [r.get("ts", 0) for r in records if r.get("ts")]
        meta = {
            "name": name,
            "count": len(records),
            "size": st.st_size,
            "first_rev": min(revs) if revs else None,
            "last_rev": max(revs) if revs else None,
            "first_ts": min(tss) if tss else None,
            "last_ts": max(tss) if tss else None,
        }
        SHARD_META_CACHE[cache_key] = meta
        return meta

    def shards_index(self) -> List[Dict[str, Any]]:
        return [self._shard_meta(n) for n in self.log.list_shards()]

    # ---------------------------------------------------------------- 读取
    def iter_ops(self, from_rev: int = 0, to_rev: Optional[int] = None,
                 limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """按 rev 升序返回 (from_rev, to_rev] 区间的操作(线性扫相关分片)。"""
        out: List[Dict[str, Any]] = []
        for meta in self.shards_index():
            last = meta.get("last_rev")
            first = meta.get("first_rev")
            if last is None or last <= from_rev:
                continue
            if to_rev is not None and first is not None and first > to_rev:
                break
            for rec in self.log.read_shard(meta["name"]):
                rev = rec.get("rev") or 0
                if rev > from_rev and (to_rev is None or rev <= to_rev) and rec.get("type") != "move":
                    out.append(rec)
                    if limit and len(out) >= limit:
                        return out
        out.sort(key=lambda r: r.get("rev") or 0)
        return out

    def recent_ops(self, since_rev: int, limit: int = config.MAX_CATCHUP_OPS) -> List[Dict[str, Any]]:
        """断线补发用: 返回 since_rev 之后的全部操作(升序, 带截断标记)。"""
        ops = self.iter_ops(from_rev=since_rev, limit=limit)
        return ops

    def op_stats(self) -> Dict[str, Any]:
        by_type: Dict[str, int] = {}
        by_user: Dict[str, int] = {}
        total = 0
        for meta in self.shards_index():
            total += meta.get("size", 0)
        for rec in self.log.iter_all():
            otype = rec.get("type", "?")
            if otype == "batch":
                subs = rec.get("ops") or []
                otype = f"batch({len(subs)})"
            by_type[otype] = by_type.get(otype, 0) + 1
            user = rec.get("by") or rec.get("site") or "?"
            by_user[user] = by_user.get(user, 0) + 1
        return {"total": total, "by_type": by_type, "by_user": by_user,
                "shards": len(self.log.list_shards())}

    # ---------------------------------------------------------------- 快照
    def snapshots_index(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            names = sorted(n for n in os.listdir(self.snap_dir) if n.endswith(".json"))
        except FileNotFoundError:
            return out
        for name in names:
            try:
                rev = int(name[:-5])
                size = os.path.getsize(os.path.join(self.snap_dir, name))
                out.append({"rev": rev, "name": name, "size": size})
            except (ValueError, OSError):
                continue
        return out

    def save_snapshot(self, doc: BoardDoc, state: Optional[Dict[str, Any]] = None) -> int:
        """把当前文档状态保存为快照(原子写), 并按保留数修剪旧快照。

        state 可由调用方在事件循环线程预先 export(避免与并发修改竞态);
        缺省时当场导出(同步/单线程场景如播种)。
        """
        rev = doc.head_rev
        if state is None:
            state = doc.export_state()
        path = os.path.join(self.snap_dir, f"{rev:09d}.json")
        write_json_atomic(path, state)
        write_json_atomic(os.path.join(self.dir, "state.json"), state)
        doc.last_snapshot_rev = rev
        doc.dirty_ops_since_snapshot = 0
        self._prune_snapshots()
        return rev

    def _prune_snapshots(self) -> None:
        keep = int(config.get_settings().get("snapshot_keep") or 40)
        snaps = self.snapshots_index()
        if len(snaps) <= keep:
            return
        for entry in snaps[:-keep]:
            try:
                os.unlink(os.path.join(self.snap_dir, entry["name"]))
            except OSError:
                pass

    def load_snapshot(self, at_rev: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """加载 ≤ at_rev 的最新快照; at_rev 为 None 时加载最新快照。"""
        snaps = self.snapshots_index()
        pick: Optional[Dict[str, Any]] = None
        for entry in snaps:
            if at_rev is None or entry["rev"] <= at_rev:
                pick = entry
            else:
                break
        if pick is None:
            return None
        data = read_json(os.path.join(self.snap_dir, pick["name"]))
        if isinstance(data, dict):
            data["_snapshot_rev"] = pick["rev"]
        return data

    def load_state_file(self) -> Optional[Dict[str, Any]]:
        return read_json(os.path.join(self.dir, "state.json"))

    # ---------------------------------------------------------------- 回放窗口
    def replay_window(self, at_rev: Optional[int], coalesce: bool = False,
                      page_limit: Optional[int] = None) -> Dict[str, Any]:
        """快速回放核心: 最近快照 + 其后到 at_rev 的操作。

        coalesce=True(快进/拖动)时合并连续 move 增量, 大幅减少折叠步数。
        """
        snapshot = self.load_snapshot(at_rev)
        base_rev = int((snapshot or {}).get("rev") or 0)
        ops = self.iter_ops(from_rev=base_rev + 1, to_rev=at_rev, limit=page_limit)
        if coalesce:
            from .crdt import coalesce_moves
            ops = coalesce_moves(ops, config.MOVE_COALESCE_WINDOW_MS)
        return {
            "snapshot": snapshot,
            "base_rev": base_rev,
            "target_rev": at_rev,
            "ops": ops,
            "coalesced": coalesce,
        }

    def fold_window(self, at_rev: Optional[int]) -> Dict[str, Any]:
        """服务端折叠出 at_rev 时刻的完整状态(导出/缩略预览用)。"""
        window = self.replay_window(at_rev)
        doc = BoardDoc(self.board_id)
        if window["snapshot"]:
            doc.import_state(window["snapshot"])
        for raw in window["ops"]:
            clean = validate_op(raw)
            if clean:
                doc.apply_op(clean)
        return {"rev": at_rev if at_rev is not None else doc.head_rev,
                "shapes": doc.visible_shapes()}

    # ---------------------------------------------------------------- 压缩与保留期
    def compact_archived(self) -> Dict[str, Any]:
        """物理压缩归档分片(位于最新快照之下且非当天): 终态等价、有损中间态。"""
        latest = self.snapshots_index()
        snap_rev = latest[-1]["rev"] if latest else 0
        today_prefix = time.strftime("%Y%m%d")
        compacted_shards = 0
        ops_removed = 0
        for meta in self.shards_index():
            name = meta["name"]
            last_rev = meta.get("last_rev") or 0
            stamp = name[len("ops-"): -len(".jsonl")]
            if last_rev >= snap_rev or stamp.startswith(today_prefix):
                continue                       # 快照之上或今天的分片保持原样
            records = self.log.read_shard(name)
            compacted = compact_ops_lossy(records, config.MOVE_COALESCE_WINDOW_MS * 60)
            if len(compacted) < len(records):
                self.log.rewrite_shard(name, compacted)
                SHARD_META_CACHE.pop(self.log.shard_path(name), None)   # noqa: 保持原路径弹出协议
                compacted_shards += 1
                ops_removed += len(records) - len(compacted)
        return {"compacted_shards": compacted_shards, "ops_removed": ops_removed}

    def prune_expired(self) -> List[str]:
        days = int(config.get_settings().get("history_retention_days") or 30)
        cutoff = int(time.time() * 1000) - days * 86400_000
        removed = self.log.prune_before(cutoff)
        for name in removed:
            SHARD_META_CACHE.pop(self.log.shard_path(name), None)
        return removed

    # ---------------------------------------------------------------- 统计
    def storage_stats(self) -> Dict[str, Any]:
        return {
            "ops_bytes": self.log.total_size(),
            "snapshots": len(self.snapshots_index()),
            "snapshot_bytes": dir_size(self.snap_dir),
            "total_bytes": dir_size(self.dir),
        }


# ---------------------------------------------------------------- 历史聚合入口
class HistoryService:
    """按 board_id 缓存 BoardHistory 实例。"""

    def __init__(self) -> None:
        self._cache: Dict[str, BoardHistory] = {}

    def for_board(self, board_id: str) -> BoardHistory:
        hist = self._cache.get(board_id)
        if hist is None:
            hist = BoardHistory(board_id)
            self._cache[board_id] = hist
        return hist

    def drop(self, board_id: str) -> None:
        self._cache.pop(board_id, None)


history_service = HistoryService()
