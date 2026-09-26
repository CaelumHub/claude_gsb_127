"""白板生命周期管理 + REST 路由。

BoardManager 职责:
- 内存热文档缓存: 白板首次被访问时从「最新快照 + 其后操作日志」重建
  BoardDoc; 长时间无连接自动 flush 并卸载(LRU), 控制内存占用。
- 操作接入管线(WS 与 REST 共用): 校验 → per-board 锁内分配全序 rev →
  应用 CRDT → 追加分片日志 → (达到阈值时)触发快照。
- meta.json 保存 ACL/名称/模式/缩略图, _index.json 作为列表页缓存。
"""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException

from . import auth, config
from .crdt import BoardDoc, validate_op
from .history import history_service
from .models import (BoardCreateReq, BoardPatchReq, DuplicateReq,
                     PermissionsReq)
from .storage import (now_ms, read_json, safe_id, write_json_atomic,
                      write_json_atomic_async)

router = APIRouter(prefix="/api/boards", tags=["boards"])


def new_board_id() -> str:
    return "b" + secrets.token_hex(6)


def new_op_id_site() -> str:
    return "srv-" + secrets.token_hex(4)


class BoardManager:
    def __init__(self) -> None:
        self.docs: Dict[str, BoardDoc] = {}
        self.metas: Dict[str, Dict[str, Any]] = {}
        self.locks: Dict[str, asyncio.Lock] = {}
        self.load_locks: Dict[str, asyncio.Lock] = {}
        self.last_touch: Dict[str, float] = {}
        self.pending_snapshot: Dict[str, bool] = {}
        self._index_loaded = False

    # ------------------------------------------------------------ 锁
    def lock_for(self, board_id: str) -> asyncio.Lock:
        lock = self.locks.get(board_id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[board_id] = lock
        return lock

    def touch(self, board_id: str) -> None:
        self.last_touch[board_id] = time.time()

    # ------------------------------------------------------------ meta
    def _meta_path(self, board_id: str) -> str:
        return os.path.join(config.board_dir(board_id), "meta.json")

    def load_index(self) -> None:
        """启动时把磁盘上所有白板的 meta 读进内存(缺失索引则重建)。"""
        if self._index_loaded:
            return
        self._index_loaded = True
        index = read_json(config.BOARD_INDEX_FILE, default={}) or {}
        boards = index.get("boards") if isinstance(index, dict) else None
        if not isinstance(boards, dict):
            boards = {}
        try:
            entries = os.listdir(config.BOARDS_DIR)
        except FileNotFoundError:
            entries = []
        for name in entries:
            path = os.path.join(config.BOARDS_DIR, name)
            if not os.path.isdir(path) or name.startswith("_") or name.startswith("."):
                continue
            meta = read_json(os.path.join(path, "meta.json"), default=None)
            if isinstance(meta, dict) and meta.get("id"):
                self.metas[meta["id"]] = meta
                boards[meta["id"]] = self._index_entry(meta)
        self._write_index(boards)

    @staticmethod
    def _index_entry(meta: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": meta.get("id"), "name": meta.get("name"), "mode": meta.get("mode"),
            "owner": meta.get("owner"), "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"), "tags": meta.get("tags") or [],
        }

    def _write_index(self, boards: Optional[Dict[str, Any]] = None) -> None:
        if boards is None:
            boards = {bid: self._index_entry(m) for bid, m in self.metas.items()}
        write_json_atomic(config.BOARD_INDEX_FILE, {"boards": boards, "updated_at": now_ms()})

    def get_meta(self, board_id: str) -> Optional[Dict[str, Any]]:
        self.load_index()
        meta = self.metas.get(board_id)
        if meta is None:
            # 索引可能落后于磁盘(手工拷贝目录等), 兜底再读一次
            disk = read_json(self._meta_path(board_id), default=None)
            if isinstance(disk, dict) and disk.get("id"):
                self.metas[board_id] = disk
                meta = disk
        return meta

    async def save_meta(self, board_id: str) -> None:
        meta = self.metas.get(board_id)
        if meta is None:
            return
        await write_json_atomic_async(self._meta_path(board_id), meta)
        boards = {bid: self._index_entry(m) for bid, m in self.metas.items()}
        await write_json_atomic_async(config.BOARD_INDEX_FILE,
                                      {"boards": boards, "updated_at": now_ms()})

    # ------------------------------------------------------------ 文档装载
    def load_lock_for(self, board_id: str) -> asyncio.Lock:
        """装载锁(独立于操作锁): 防止并发 get_doc 双重构建互相覆盖。"""
        lock = self.load_locks.get(board_id)
        if lock is None:
            lock = asyncio.Lock()
            self.load_locks[board_id] = lock
        return lock

    async def get_doc(self, board_id: str) -> BoardDoc:
        """获取内存文档; 未加载则从快照+日志重建(双检锁, 重建仅发生一次)。"""
        doc = self.docs.get(board_id)
        if doc is not None:
            self.touch(board_id)
            return doc
        async with self.load_lock_for(board_id):
            doc = self.docs.get(board_id)
            if doc is not None:              # 等锁期间他人已完成装载
                self.touch(board_id)
                return doc
            doc = await self._rebuild_doc(board_id)
            self.docs[board_id] = doc
            self.touch(board_id)
            return doc

    async def _rebuild_doc(self, board_id: str) -> BoardDoc:
        doc = BoardDoc(board_id)
        hist = history_service.for_board(board_id)
        loop = asyncio.get_running_loop()

        def _rebuild() -> BoardDoc:
            snapshot = hist.load_snapshot()
            if snapshot:
                doc.import_state(snapshot)
            base_rev = doc.head_rev
            max_rev = base_rev
            for raw in hist.iter_ops(from_rev=base_rev + 1):
                clean = validate_op(raw)
                if clean:
                    doc.apply_op(clean)
                max_rev = max(max_rev, int(raw.get("rev") or 0))
            # apply_op 自增的 head_rev 与日志真实 rev 对齐(重建路径上二者一致,
            # 兜底取 max, 防止日志中有被校验丢弃的操作导致序号错位)
            doc.head_rev = max(doc.head_rev, max_rev)
            doc.last_snapshot_rev = base_rev
            doc.dirty_ops_since_snapshot = 0
            return doc

        return await loop.run_in_executor(None, _rebuild)

    async def unload_idle(self) -> List[str]:
        """卸载长时间无访问的白板文档, 释放内存。"""
        idle_secs = int(config.get_settings().get("board_idle_unload_secs") or 1800)
        now = time.time()
        unloaded: List[str] = []
        for board_id in list(self.docs.keys()):
            if now - self.last_touch.get(board_id, now) < idle_secs:
                continue
            doc = self.docs.pop(board_id)
            try:
                hist = history_service.for_board(board_id)
                if doc.dirty_ops_since_snapshot > 0:
                    state = doc.export_state()      # 循环线程内导出, 避免竞态
                    await asyncio.get_running_loop().run_in_executor(
                        None, hist.save_snapshot, doc, state)
            except Exception:                                   # noqa: BLE001
                pass
            unloaded.append(board_id)
        return unloaded

    def is_loaded(self, board_id: str) -> bool:
        return board_id in self.docs

    # ------------------------------------------------------------ 操作接入
    async def ingest_ops(self, board_id: str, raw_ops: List[Any],
                         by: str = "", site_hint: str = "") -> List[Dict[str, Any]]:
        """校验并接入一批客户端操作: 分配 rev → 应用 → 落日志。

        返回带 rev 的已接受操作列表(非法操作被静默丢弃并在返回值中缺席,
        WS 层负责回 error)。同一白板内由 per-board 锁保证 rev 分配与
        日志追加的顺序一致(并发写入原子性)。
        """
        settings = config.get_settings()
        max_batch = int(settings.get("max_ops_per_batch") or 64)
        accepted: List[Dict[str, Any]] = []
        async with self.lock_for(board_id):
            doc = await self.get_doc(board_id)
            for raw in raw_ops[:max_batch]:
                clean = validate_op(raw)
                if clean is None:
                    continue
                # 图元数量上限(add_shape 计入)
                if clean["type"] == "add_shape" and \
                        len(doc.shapes) >= int(settings.get("max_shapes_per_board") or 8000):
                    continue
                if doc.seen_rev(clean["op_id"]) is not None:
                    # 幂等: 断线重连后客户端重发, 直接回原 rev 的 ack
                    dup = dict(clean)
                    dup["rev"] = doc.seen_rev(clean["op_id"])
                    dup["dup"] = True
                    accepted.append(dup)
                    continue
                rev, _applied = doc.apply_op(clean, by=by)
                accepted.append(clean)
            if accepted:
                hist = history_service.for_board(board_id)
                stamped = [op for op in accepted if "rev" in op and op.get("type") != "move"]
                await asyncio.get_running_loop().run_in_executor(
                    None, hist.append_ops, stamped)
                meta = self.metas.get(board_id)
                if meta is not None:
                    meta["updated_at"] = now_ms()
                    stats = doc.stats()
                    meta["stats"] = {"rev": stats["rev"], "shapes": len(doc.shapes)}
            self.touch(board_id)
        if accepted:
            self.pending_snapshot[board_id] = True
        return accepted

    async def maybe_snapshot(self, board_id: str) -> Optional[int]:
        """达到阈值(操作数或时间)时保存快照。返回快照 rev 或 None。"""
        doc = self.docs.get(board_id)
        if doc is None or not self.pending_snapshot.get(board_id):
            return None
        settings = config.get_settings()
        ops_gap = doc.head_rev - doc.last_snapshot_rev
        time_gap = (now_ms() - (doc.last_op_ts or now_ms())) / 1000.0
        enough_ops = ops_gap >= int(settings.get("snapshot_interval_ops") or 200)
        enough_time = ops_gap >= 20 and doc.last_op_ts and \
            (time.time() - self.last_touch.get(board_id, time.time())) > 5 and \
            ops_gap > 0 and self._snapshot_timer_due(board_id, int(settings.get("snapshot_interval_secs") or 600))
        if not (enough_ops or enough_time):
            return None
        self.pending_snapshot[board_id] = False
        hist = history_service.for_board(board_id)
        state = doc.export_state()              # 循环线程内导出, 写盘进线程池
        rev = await asyncio.get_running_loop().run_in_executor(
            None, hist.save_snapshot, doc, state)
        await self.save_meta(board_id)
        return rev

    _snapshot_timers: Dict[str, float] = {}

    def _snapshot_timer_due(self, board_id: str, interval_secs: int) -> bool:
        last = self._snapshot_timers.get(board_id, 0)
        if time.time() - last >= interval_secs:
            self._snapshot_timers[board_id] = time.time()
            return True
        return False

    async def force_snapshot(self, board_id: str) -> int:
        doc = await self.get_doc(board_id)
        hist = history_service.for_board(board_id)
        state = doc.export_state()
        rev = await asyncio.get_running_loop().run_in_executor(
            None, hist.save_snapshot, doc, state)
        await self.save_meta(board_id)
        self.pending_snapshot[board_id] = False
        return rev

    # ------------------------------------------------------------ 创建/删除
    async def create_board(self, name: str, mode: str, owner: str,
                           template_id: Optional[str] = None,
                           tags: Optional[List[str]] = None) -> Dict[str, Any]:
        self.load_index()
        board_id = new_board_id()
        while os.path.isdir(config.board_dir(board_id)):
            board_id = new_board_id()
        now = now_ms()
        meta: Dict[str, Any] = {
            "id": board_id,
            "name": (name or "未命名白板")[:80],
            "mode": mode if mode in ("board", "mindmap") else "board",
            "owner": owner,
            "acl": {},
            "public_role": None,
            "tags": [str(t)[:20] for t in (tags or [])][:10],
            "created_at": now,
            "updated_at": now,
            "created_by": owner,
            "thumbnail": None,
            "stats": {"rev": 0, "shapes": 0},
        }
        os.makedirs(config.board_dir(board_id), exist_ok=True)
        self.metas[board_id] = meta
        await self.save_meta(board_id)
        doc = BoardDoc(board_id)
        self.docs[board_id] = doc
        self.touch(board_id)

        if template_id:
            from . import templates as tpl
            shapes = tpl.build_template_shapes(template_id, mode=meta["mode"])
            if shapes:
                site = new_op_id_site()
                ops: List[Dict[str, Any]] = []
                for i, shape in enumerate(shapes):
                    ops.append({
                        "op_id": f"{site}:{i}", "site": site, "lam": i + 1,
                        "ts": now + i, "type": "add_shape", "base_rev": 0,
                        "shape": shape,
                    })
                await self.ingest_ops(board_id, ops, by=owner)
        await self.force_snapshot(board_id)
        return meta

    async def delete_board(self, board_id: str) -> None:
        import shutil
        self.docs.pop(board_id, None)
        self.metas.pop(board_id, None)
        history_service.drop(board_id)
        await asyncio.get_running_loop().run_in_executor(
            None, shutil.rmtree, config.board_dir(board_id), True)
        await self.save_meta(board_id)   # metas 已删 → 重写索引

    async def duplicate_board(self, board_id: str, new_name: Optional[str],
                              owner: str) -> Dict[str, Any]:
        src_meta = self.get_meta(board_id)
        if src_meta is None:
            raise KeyError(board_id)
        doc = await self.get_doc(board_id)
        state = doc.export_state()
        meta = await self.create_board(new_name or f"{src_meta.get('name')} 副本",
                                       src_meta.get("mode", "board"), owner,
                                       tags=src_meta.get("tags"))
        # 用新 ID 重建图形(避免与源白板 op_id 冲突)
        site = new_op_id_site()
        ops: List[Dict[str, Any]] = []
        i = 0
        id_map: Dict[str, str] = {}
        for shape in state["shapes"].values():
            if shape.get("deleted"):
                continue
            new_shape = dict(shape)
            new_shape["id"] = "sh" + secrets.token_hex(8)
            id_map[shape["id"]] = new_shape["id"]
            new_shape.pop("fc", None)
            ops.append({"op_id": f"{site}:{i}", "site": site, "lam": i + 1,
                        "ts": now_ms() + i, "type": "add_shape", "shape": new_shape})
            i += 1
        for op in ops:   # 连线/父子关系映射到新 ID
            shape = op["shape"]
            for field in ("from", "to"):
                if shape.get(field) in id_map:
                    shape[field] = id_map[shape[field]]
        if ops:
            await self.ingest_ops(meta["id"], ops, by=owner)
        await self.force_snapshot(meta["id"])
        return meta


manager = BoardManager()


# ---------------------------------------------------------------- REST 辅助
async def board_ctx(board_id: str, user: Dict[str, Any],
                    required: str) -> Tuple[Dict[str, Any], str]:
    """取 meta + 校验角色; 返回 (meta, role)。"""
    meta = manager.get_meta(board_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="白板不存在")
    role = auth.board_role(user, meta)
    if not auth.role_at_least(role, "viewer"):
        raise HTTPException(status_code=403, detail="无权访问该白板")
    if not auth.role_at_least(role, required):
        raise HTTPException(status_code=403,
                            detail=f"需要 {required} 及以上权限(当前: {role})")
    return meta, role or "viewer"


# ---------------------------------------------------------------- REST 路由
@router.get("")
async def list_boards(q: str = "", mode: str = "", tag: str = "",
                      sort: str = "updated", user: Dict[str, Any] = Depends(auth.current_user)):
    manager.load_index()
    out: List[Dict[str, Any]] = []
    for bid, meta in manager.metas.items():
        role = auth.board_role(user, meta)
        if not auth.role_at_least(role, "viewer"):
            continue
        if q and q.lower() not in (meta.get("name") or "").lower():
            continue
        if mode and meta.get("mode") != mode:
            continue
        if tag and tag not in (meta.get("tags") or []):
            continue
        item = dict(meta)
        item["your_role"] = role
        item.pop("acl", None)
        out.append(item)
    reverse = sort == "name"
    key = {"name": lambda m: (m.get("name") or ""),
           "created": lambda m: m.get("updated_at") or 0,
           "size": lambda m: (m.get("stats") or {}).get("rev") or 0}.get(sort) \
        or (lambda m: m.get("created_at") or 0)
    out.sort(key=key, reverse=reverse)
    return {"boards": out, "count": len(out)}


@router.post("")
async def create_board(req: BoardCreateReq, user: Dict[str, Any] = Depends(auth.current_user)):
    meta = await manager.create_board(req.name, req.mode, user["username"],
                                      template_id=req.template_id, tags=req.tags)
    return {"board": meta}


@router.get("/{board_id}")
async def get_board(board_id: str, user: Dict[str, Any] = Depends(auth.current_user)):
    meta, role = await board_ctx(board_id, user, "viewer")
    return {"board": meta, "your_role": role}


@router.patch("/{board_id}")
async def patch_board(board_id: str, req: BoardPatchReq,
                      user: Dict[str, Any] = Depends(auth.current_user)):
    meta, role = await board_ctx(board_id, user, "editor")
    if req.name is not None:
        meta["name"] = req.name[:80]
    if req.mode in ("board", "mindmap"):
        meta["mode"] = req.mode
    if req.tags is not None:
        meta["tags"] = [str(t)[:20] for t in req.tags][:10]
    if req.thumbnail is not None:
        meta["thumbnail"] = req.thumbnail[:60000] if req.thumbnail else None
    meta["updated_at"] = now_ms()
    await manager.save_meta(board_id)
    return {"board": meta, "your_role": role}


@router.delete("/{board_id}")
async def delete_board(board_id: str, user: Dict[str, Any] = Depends(auth.current_user)):
    meta = manager.get_meta(board_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="白板不存在")
    if meta.get("owner") != user["username"] and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅白板所有者或管理员可删除")
    await manager.delete_board(board_id)
    return {"ok": True, "deleted": board_id}


@router.post("/{board_id}/duplicate")
async def duplicate_board(board_id: str, req: DuplicateReq,
                          user: Dict[str, Any] = Depends(auth.current_user)):
    await board_ctx(board_id, user, "viewer")
    meta = await manager.duplicate_board(board_id, req.name, user["username"])
    return {"board": meta}


@router.get("/{board_id}/state")
async def get_state(board_id: str, user: Dict[str, Any] = Depends(auth.current_user)):
    meta, role = await board_ctx(board_id, user, "viewer")
    doc = await manager.get_doc(board_id)
    return {
        "board": meta,
        "your_role": role,
        "rev": doc.head_rev,
        "shapes": doc.visible_shapes(),
        "stats": doc.stats(),
    }


@router.post("/{board_id}/snapshot")
async def force_snapshot(board_id: str, user: Dict[str, Any] = Depends(auth.current_user)):
    await board_ctx(board_id, user, "editor")
    rev = await manager.force_snapshot(board_id)
    return {"ok": True, "snapshot_rev": rev}


@router.get("/{board_id}/permissions")
async def get_permissions(board_id: str, user: Dict[str, Any] = Depends(auth.current_user)):
    meta, role = await board_ctx(board_id, user, "viewer")
    users = auth.list_users()
    return {
        "board_id": board_id,
        "owner": meta.get("owner"),
        "acl": meta.get("acl") or {},
        "public_role": meta.get("public_role"),
        "your_role": role,
        "users": [{"username": u["username"], "display_name": u["display_name"],
                   "color": u["color"], "role": u["role"]} for u in users],
        "can_manage": role == "owner",
    }


@router.put("/{board_id}/permissions")
async def put_permissions(board_id: str, req: PermissionsReq,
                          user: Dict[str, Any] = Depends(auth.current_user)):
    meta, role = await board_ctx(board_id, user, "owner")
    acl: Dict[str, str] = {}
    known = {u["username"] for u in auth.list_users()}
    for name, want in (req.acl or {}).items():
        if name in known and want in auth.VALID_ROLES and name != meta.get("owner"):
            acl[name.lower()] = want
    meta["acl"] = acl
    if req.public_role in auth.VALID_ROLES:
        meta["public_role"] = req.public_role
    elif req.public_role is not None:
        meta["public_role"] = None
    await manager.save_meta(board_id)
    return {"ok": True, "acl": meta["acl"], "public_role": meta.get("public_role")}


@router.get("/{board_id}/stats")
async def board_stats(board_id: str, user: Dict[str, Any] = Depends(auth.current_user)):
    await board_ctx(board_id, user, "viewer")
    doc = await manager.get_doc(board_id)
    hist = history_service.for_board(board_id)
    storage = hist.storage_stats()
    return {
        "doc": doc.stats(),
        "loaded_in_memory": manager.is_loaded(board_id),
        "storage": storage,
        "shards": hist.shards_index()[-24:],
        "snapshots": hist.snapshots_index()[-10:],
    }
