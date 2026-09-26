"""操作型 CRDT 引擎 —— 图形协同冲突解决。

设计要点(对应「同时移动同一元素」等冲突场景):

* **移动 = 增量(delta), 天然可交换**
  move 操作携带 (dx, dy) 而非绝对坐标。两个用户同时拖动同一图形时,
  两个增量在任意到达顺序下都相加合成(PN-Counter 语义), 最终位置 =
  原位 + ΔA + ΔB —— 不会互相覆盖, 也不需要 OT 变换。这是并发移动
  冲突的根本解法。

* **属性 = 字段级 LWW-Register**
  fill/text/w 等非增量属性, 每个字段挂一个逻辑时钟 (lamport, site)。
  set_props 仅当时钟更大才生效; 平票按 site 字典序决胜 —— 所有副本
  在任意投递顺序下收敛到同一值。并发修改同一图形的不同字段互不干扰。

* **删除 = LWW 墓碑 + 乱序容忍**
  delete/restore 都是 "deleted" 字段上的带时钟寄存器。delete 先于
  add 到达时创建墓碑占位, 之后的 add 只有在时钟更大时才复活图形,
  保证乱序投递(断线补发)下依然收敛。

* **幂等与去重**
  每个操作带全局唯一 op_id = "site:seq"; 服务端记忆最近 8192 个
  op_id, 断线重连后客户端重发队列里的操作会被去重并返回原 rev 的
  ack, 不会二次生效。

* **批量与撤销**
  batch 把多个子操作打包为一个 rev / 一个撤销单元。撤销不是回滚,
  而是发布语义逆操作(move→反向move, add→delete, set_props→写回
  旧值), 因此与他人的并发操作天然可合并; redo 用新时钟重放原变更。

操作信封:
    {op_id, site, lam, ts, type, base_rev, ...payload}
服务端接受后追加: {rev, by}
"""
from __future__ import annotations

import math
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 常量与白名单
SHAPE_KINDS = {
    "path", "line", "arrow", "rect", "ellipse", "diamond",
    "note", "text", "edge", "mindnode", "ghost",
}

OP_TYPES = {
    "add_shape", "set_props", "move", "delete_shape", "restore_shape",
    "reorder", "reparent", "path_extend", "truncate_path", "batch",
}

#: 允许通过 set_props 走 LWW 寄存器修改的字段
LWW_FIELDS = {
    "fill", "stroke", "strokeWidth", "fontSize", "fontFamily", "fontWeight",
    "fontStyle", "opacity", "dash", "text", "textColor", "cornerRadius",
    "collapsed", "from", "to", "waypoints", "w", "h", "x", "y", "name",
    "tag", "strokeStyle", "arrowStart", "arrowEnd",
}

NUMERIC_FIELDS = {"x", "y", "w", "h", "rotation", "z", "strokeWidth", "fontSize", "opacity", "cornerRadius"}

MAX_TEXT_LEN = 8000
MAX_POINTS = 20000
MAX_PROP_KEY_LEN = 40
COORD_LIMIT = 2_000_000
SEEN_OPS_LIMIT = 8192
DEFAULT_Z = 1.0

Clock = Tuple[int, str]   # (lamport, site)


def clock_gt(a: Optional[Clock], b: Optional[Clock]) -> bool:
    """LWW 时钟比较: (lamport, site) 字典序; None 视为最旧。"""
    if a is None:
        return False
    if b is None:
        return True
    if a[0] != b[0]:
        return a[0] > b[0]
    return a[1] > b[1]


def _finite_number(value: Any, limit: float = COORD_LIMIT) -> Optional[float]:
    """清洗数值: 拒绝 inf/nan, 限幅, 并量化到 6 位小数。

    量化保证 move 增量求和与到达顺序无关(浮点加法本身不满足结合律,
    但「6 位小数值 + 6 位小数值再舍入到 6 位」在双精度下是确定的)。
    客户端 CrdtClient 的构造器做同样量化, 两端语义一致。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return round(max(-limit, min(limit, num)), 6)


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------- 操作构造/校验
def make_op(op_type: str, site: str, lam: int, payload: Dict[str, Any],
            seq: int = 0, ts: Optional[int] = None, base_rev: int = 0) -> Dict[str, Any]:
    op: Dict[str, Any] = {
        "op_id": f"{site}:{seq or lam}",
        "site": site,
        "lam": int(lam),
        "ts": int(ts if ts is not None else now_ms()),
        "type": op_type,
        "base_rev": int(base_rev or 0),
    }
    op.update(payload)
    return op


def sanitize_shape(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """清洗一个待入库的图形对象; 非法返回 None。"""
    if not isinstance(raw, dict):
        return None
    sid = str(raw.get("id") or "")[:64]
    kind = str(raw.get("kind") or "")
    if not sid or kind not in SHAPE_KINDS:
        return None
    shape: Dict[str, Any] = {"id": sid, "kind": kind}
    for field in ("x", "y", "w", "h", "rotation", "z"):
        num = _finite_number(raw.get(field))
        if num is not None:
            shape[field] = num
    shape.setdefault("x", 0.0)
    shape.setdefault("y", 0.0)
    shape.setdefault("z", DEFAULT_Z)
    text = raw.get("text")
    if isinstance(text, str):
        shape["text"] = text[:MAX_TEXT_LEN]
    points = raw.get("points")
    if isinstance(points, list):
        clean_pts: List[List[float]] = []
        for pt in points[:MAX_POINTS]:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                px, py = _finite_number(pt[0]), _finite_number(pt[1])
                if px is not None and py is not None:
                    clean_pts.append([px, py])
        if clean_pts:
            shape["points"] = clean_pts
    for key in ("fill", "stroke", "textColor", "fontFamily", "name", "tag", "strokeStyle"):
        val = raw.get(key)
        if isinstance(val, str):
            shape[key] = val[:120]
    for key in ("strokeWidth", "fontSize", "opacity", "cornerRadius"):
        num = _finite_number(raw.get(key), 10000)
        if num is not None:
            shape[key] = num
    dash = raw.get("dash")
    if isinstance(dash, list):
        shape["dash"] = [_finite_number(d, 500) or 0 for d in dash[:8]]
    collapsed = raw.get("collapsed")
    if isinstance(collapsed, bool):
        shape["collapsed"] = collapsed
    for key in ("from", "to"):
        val = raw.get(key)
        if isinstance(val, str) and val:
            shape[key] = val[:64]
    meta = raw.get("meta")
    if isinstance(meta, dict):
        shape["meta"] = {
            "author": str(meta.get("author") or "")[:60],
            "createdAt": int(meta.get("createdAt") or now_ms()),
        }
    fc = raw.get("fc")
    if isinstance(fc, dict):
        shape["fc"] = {str(k)[:MAX_PROP_KEY_LEN]: [int(v[0]), str(v[1])[:64]]
                       for k, v in fc.items()
                       if isinstance(v, (list, tuple)) and len(v) == 2}
    return shape


def sanitize_props(raw: Dict[str, Any]) -> Dict[str, Any]:
    """清洗 set_props 的属性字典, 只保留白名单字段。

    值为 None 表示「复位该字段」(撤销时写回不存在的旧值), 原样保留;
    客户端渲染端把 null 视作缺省。两端语义一致, 保证收敛。
    """
    out: Dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in list(raw.items())[:64]:
        key = str(key)[:MAX_PROP_KEY_LEN]
        if key not in LWW_FIELDS:
            continue
        if value is None:
            out[key] = None
            continue
        if key in NUMERIC_FIELDS:
            num = _finite_number(value, 100000)
            if num is not None:
                out[key] = num
        elif key == "text":
            if isinstance(value, str):
                out[key] = value[:MAX_TEXT_LEN]
        elif key == "collapsed":
            if isinstance(value, bool):
                out[key] = value
        elif key == "dash":
            if isinstance(value, list):
                out[key] = [_finite_number(d, 500) or 0 for d in value[:8]]
        elif key in ("from", "to"):
            if isinstance(value, (str, type(None))):
                out[key] = str(value or "")[:64]
        elif isinstance(value, (str, bool, int, float)):
            out[key] = value if not isinstance(value, str) else value[:200]
    return out


def sanitize_points(raw: Any) -> List[List[float]]:
    pts: List[List[float]] = []
    if isinstance(raw, list):
        for pt in raw[:MAX_POINTS]:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                px, py = _finite_number(pt[0]), _finite_number(pt[1])
                if px is not None and py is not None:
                    pts.append([px, py])
    return pts


def validate_op(op: Any) -> Optional[Dict[str, Any]]:
    """校验并规范化客户端提交的操作; 非法返回 None。"""
    if not isinstance(op, dict):
        return None
    op_type = op.get("type")
    if op_type not in OP_TYPES:
        return None
    op_id = str(op.get("op_id") or "")[:96]
    site = str(op.get("site") or "")[:64]
    if not op_id or not site:
        return None
    try:
        lam = int(op.get("lam") or 0)
    except (TypeError, ValueError):
        return None
    if lam < 0 or lam > 2**53:
        return None
    clean: Dict[str, Any] = {
        "op_id": op_id, "site": site, "lam": lam,
        "ts": int(_finite_number(op.get("ts"), 2**42) or now_ms()),
        "type": op_type,
        "base_rev": int(_finite_number(op.get("base_rev"), 2**40) or 0),
    }
    if op_type == "batch":
        subs = op.get("ops")
        if not isinstance(subs, list) or not subs or len(subs) > 512:
            return None
        clean_subs = []
        for sub in subs:
            cs = validate_op(sub)
            if cs and cs["type"] != "batch":       # 禁止嵌套 batch
                clean_subs.append(cs)
        if not clean_subs:
            return None
        clean["ops"] = clean_subs
        return clean
    if op_type == "add_shape":
        shape = sanitize_shape(op.get("shape") or {})
        if shape is None:
            return None
        clean["shape"] = shape
    elif op_type in ("delete_shape", "restore_shape"):
        target = str(op.get("id") or "")[:64]
        if not target:
            return None
        clean["id"] = target
    elif op_type == "move":
        target = str(op.get("id") or "")[:64]
        dx = _finite_number(op.get("dx"))
        dy = _finite_number(op.get("dy"))
        if not target or dx is None or dy is None:
            return None
        if dx == 0 or dy == 0:
            return None                            # 空移动直接丢弃
        clean.update({"id": target, "dx": dx, "dy": dy})
    elif op_type == "set_props":
        target = str(op.get("id") or "")[:64]
        props = sanitize_props(op.get("props") or {})
        if not target or not props:
            return None
        clean.update({"id": target, "props": props})
    elif op_type in ("reorder",):
        target = str(op.get("id") or "")[:64]
        z = _finite_number(op.get("z"), 1e9)
        if not target or z is None:
            return None
        clean.update({"id": target, "z": z})
    elif op_type == "reparent":
        target = str(op.get("id") or "")[:64]
        parent = op.get("parent")
        if not target or not isinstance(parent, (str, type(None))):
            return None
        clean.update({"id": target, "parent": str(parent or "")[:64]})
    elif op_type == "path_extend":
        target = str(op.get("id") or "")[:64]
        pts = sanitize_points(op.get("points"))
        if not target or not pts:
            return None
        clean.update({"id": target, "points": pts})
    elif op_type == "truncate_path":
        target = str(op.get("id") or "")[:64]
        length = _finite_number(op.get("length"), MAX_POINTS)
        if not target or length is None or length < 0:
            return None
        clean.update({"id": target, "length": int(length)})
    return clean


# ---------------------------------------------------------------- 白板文档状态
class BoardDoc:
    """单个白板的权威 CRDT 状态。

    - shapes: id → 图形(含字段时钟 fc)
    - head_rev: 服务端全序修订号(仅用于日志/补发定位, 收敛不依赖它)
    - _seen: 最近 op_id → rev, 用于幂等去重
    """

    def __init__(self, board_id: str):
        self.board_id = board_id
        self.shapes: Dict[str, Dict[str, Any]] = {}
        self.head_rev: int = 0
        self.lam_witness: int = 0
        self._seen: "OrderedDict[str, int]" = OrderedDict()
        self._max_z: float = 0.0
        self.dirty_ops_since_snapshot: int = 0
        self.last_op_ts: int = 0
        self.last_snapshot_rev: int = 0

    # -- 去重登记 ---------------------------------------------------------
    def _mark_seen(self, op_id: str, rev: int) -> None:
        self._seen[op_id] = rev
        self._seen.move_to_end(op_id)
        while len(self._seen) > SEEN_OPS_LIMIT:
            self._seen.popitem(last=False)

    def seen_rev(self, op_id: str) -> Optional[int]:
        return self._seen.get(op_id)

    # -- 字段时钟 ---------------------------------------------------------
    @staticmethod
    def _fc_get(shape: Dict[str, Any], field: str) -> Optional[Clock]:
        entry = (shape.get("fc") or {}).get(field)
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            return (int(entry[0]), str(entry[1]))
        return None

    @staticmethod
    def _fc_set(shape: Dict[str, Any], field: str, clock: Clock) -> None:
        fc = shape.setdefault("fc", {})
        fc[field] = [clock[0], clock[1]]

    def _lww_put(self, shape: Dict[str, Any], field: str, value: Any, clock: Clock) -> bool:
        """带时钟的寄存器写入; 返回是否生效。"""
        current = self._fc_get(shape, field)
        if not clock_gt(clock, current):
            return False
        shape[field] = value
        self._fc_set(shape, field, clock)
        return True

    # -- 单个子操作的语义应用 ----------------------------------------------
    def _apply_leaf(self, op: Dict[str, Any], clock: Clock) -> bool:
        kind = op["type"]
        if kind == "add_shape":
            shape_in = op["shape"]
            sid = shape_in["id"]
            existing = self.shapes.get(sid)
            if existing is None:
                shape = dict(shape_in)
                shape.setdefault("fc", {})
                # 为 add 携带的每个属性字段登记创建时钟(供并发 add/后续 set 比较)
                for key in list(shape.keys()):
                    if key in ("id", "fc", "meta"):
                        continue
                    self._fc_set(shape, key, clock)
                self._fc_set(shape, "deleted", clock)
                shape["deleted"] = False
                self.shapes[sid] = shape
                self._max_z = max(self._max_z, float(shape.get("z") or 0))
                return True
            # 并发/重复 add: 字段级合并
            changed = False
            # kind/meta 走 LWW: 墓碑(ghost, 无 kind 时钟)被最大时钟的 add 吸收,
            # 与到达顺序无关。
            incoming_kind = shape_in.get("kind")
            if incoming_kind in SHAPE_KINDS and \
                    clock_gt(clock, self._fc_get(existing, "kind")):
                existing["kind"] = incoming_kind
                self._fc_set(existing, "kind", clock)
                if isinstance(shape_in.get("meta"), dict):
                    existing["meta"] = shape_in["meta"]
                changed = True
            for key, value in shape_in.items():
                if key in ("id", "fc", "meta", "kind"):
                    continue
                if key in ("x", "y"):
                    # 位置基座注入: x/y 属于「增量域」(move 无时钟累加)。
                    # 仅当该字段尚未物化(占位上没有时钟)时, 把 add 携带的
                    # 基座坐标加进已累计的增量上 —— add 迟到也不丢位移;
                    # 已物化则跳过(重做 re-add 不重置位置)。
                    if self._fc_get(existing, key) is None:
                        existing[key] = round(
                            float(existing.get(key) or 0) + float(value or 0), 6)
                        self._fc_set(existing, key, clock)
                        changed = True
                    continue
                if self._lww_put(existing, key, value, clock):
                    changed = True
            # 复活判定统一走 deleted 字段的 LWW(add 携带 deleted=False):
            # 无论哪个 add 先到, fc[deleted] 都收敛到最大时钟, 后续
            # delete/restore 的比较基准在副本间一致。
            if self._lww_put(existing, "deleted", False, clock):
                changed = True
            return changed

        target_id = op.get("id")
        if not target_id:
            return False

        # 所有按 id 寻址的操作都走「延迟物化」: 目标不存在时先创建 ghost
        # 占位并记录字段时钟 —— 乱序投递(delete/set_props 先于 add 到达)
        # 与断线补发下, 后到的 add 按时钟合并, 副本间仍严格收敛。
        shape = self.shapes.get(target_id)
        if shape is None:
            shape = self._placeholder(target_id)

        if kind == "move":
            # 增量对已删除图形同样累计(复活后位置正确, 且满足交换律)
            shape["x"] = round(float(shape.get("x") or 0) + float(op["dy"]), 6)
            shape["y"] = round(float(shape.get("y") or 0) + float(op["dx"]), 6)
            return True

        if kind == "path_extend":
            pts = shape.setdefault("points", [])
            room = MAX_POINTS - len(pts)
            if room > 0:
                pts.extend(op["points"][:room])
                return True
            return False

        if kind == "truncate_path":
            # 撤销 path_extend 的逆操作: 截回绘制途中的点长度(仅作者会发出)
            pts = shape.get("points") or []
            length = max(0, int(op.get("length") or 0))
            if len(pts) <= length:
                return False
            shape["points"] = pts[:length]
            return True

        if kind in ("delete_shape", "restore_shape"):
            return self._lww_put(shape, "deleted", kind == "delete_shape", clock)

        if kind == "reorder":
            if self._lww_put(shape, "z", float(op["z"]), clock):
                self._max_z = max(self._max_z, float(op["z"]))
                return True
            return False

        if kind == "reparent":
            parent = op.get("parent") or None
            if parent == target_id:
                return False                       # 禁止自环
            if parent and self._creates_cycle(target_id, parent):
                return False                       # 禁止成环(思维导图层级)
            return self._lww_put(shape, "parent", parent, clock)

        if kind == "set_props":
            changed = False
            for field, value in op.get("props", {}).items():
                if field in ("parent",):           # parent 走 reparent(带环检测)
                    continue
                if field == "z" and value is not None:
                    if self._lww_put(shape, "z", float(value), clock):
                        self._max_z = max(self._max_z, float(value))
                        changed = True
                    continue
                if self._lww_put(shape, field, value, clock):
                    changed = True
            return changed

        return False

    def _placeholder(self, target_id: str) -> Dict[str, Any]:
        """未知 id 的延迟物化占位: 不可见(ghost+deleted), 但携带字段时钟。"""
        ph: Dict[str, Any] = {
            "id": target_id, "kind": "ghost", "deleted": True,
            "fc": {}, "x": 0.0, "y": 0.0, "z": DEFAULT_Z,
        }
        self.shapes[target_id] = ph
        return ph

    def _creates_cycle(self, node_id: str, new_parent: Optional[str]) -> bool:
        """把 node 挂到 new_parent 下是否会形成环。"""
        cursor = new_parent
        hops = 0
        while cursor and hops < 1000:
            if cursor == node_id:
                return True
            parent_shape = self.shapes.get(cursor)
            if parent_shape is None:
                return False
            cursor = parent_shape.get("parent")
            hops += 1
        return False

    # -- 对外主入口 ---------------------------------------------------------
    def apply_op(self, op: Dict[str, Any], by: str = "") -> Tuple[int, bool]:
        """应用一个(已清洗的)操作。返回 (rev, applied)。

        幂等: 重复 op_id 返回原 rev 且 applied=False。
        全序: 服务端按到达顺序分配递增 rev(仅用于日志定位)。
        """
        op_id = op["op_id"]
        prev = self._seen.get(op_id)
        if prev is not None:
            return prev, False
        self.head_rev += 1
        rev = self.head_rev
        # 原地打标 rev/by: 调用方(ingest_ops/seed)直接把同一对象写日志与广播
        op["rev"] = rev
        if by:
            op["by"] = by
        clock: Clock = (int(op.get("lam") or 0), str(op.get("site") or ""))
        self.lam_witness = max(self.lam_witness, clock[0])
        applied = False
        if op["type"] == "batch":
            for sub in op.get("ops", []):
                sub_clock = (int(sub.get("lam") or 0), str(sub.get("site") or clock[1]))
                if self._apply_leaf(sub, sub_clock):
                    applied = True
        else:
            applied = self._apply_leaf(op, clock)
        self._mark_seen(op_id, rev)
        self.dirty_ops_since_snapshot += 1
        self.last_op_ts = op.get("ts") or now_ms()
        return rev, applied

    def next_z(self) -> float:
        self._max_z += 1.0
        return self._max_z

    # -- 快照 / 恢复 --------------------------------------------------------
    def export_state(self) -> Dict[str, Any]:
        return {
            "v": 1,
            "board_id": self.board_id,
            "rev": self.head_rev,
            "lam_witness": self.lam_witness,
            "saved_at": now_ms(),
            "shapes": {sid: dict(shape) for sid, shape in self.shapes.items()},
        }

    def visible_shapes(self) -> List[Dict[str, Any]]:
        """按渲染顺序(z, 创建时间, id 稳定排序)返回可见图形。

        过滤掉墓碑与延迟物化占位(deleted 或 kind=ghost)—— 它们仍留在
        shapes 表中参与 CRDT 合并(收敛必需), 但对外不可见。
        """
        items = [s for s in self.shapes.values()
                 if not s.get("deleted") and s.get("kind") != "ghost"]
        items.sort(key=lambda s: (float(s.get("z") or 0),
                                  (s.get("meta") or {}).get("createdAt") or 0,
                                  s.get("id") or ""))
        return items

    def import_state(self, snapshot: Dict[str, Any]) -> None:
        self.shapes = {}
        raw_shapes = snapshot.get("shapes") or {}
        if isinstance(raw_shapes, dict):
            for sid, shape in raw_shapes.items():
                clean = sanitize_shape(shape if isinstance(shape, dict) else {})
                if clean is not None:
                    if isinstance(shape, dict) and shape.get("deleted"):
                        clean["deleted"] = True
                    self.shapes[clean["id"]] = clean
        self.head_rev = int(snapshot.get("rev") or 0)
        self.lam_witness = int(snapshot.get("lam_witness") or 0)
        self.last_snapshot_rev = self.head_rev
        self.dirty_ops_since_snapshot = 0
        self._max_z = max((float(s.get("z") or 0) for s in self.shapes.values()), default=0.0)
        self._seen.clear()

    def fold(self, ops: List[Dict[str, Any]]) -> int:
        """按顺序折叠一批(已清洗)操作, 返回实际生效数量。"""
        applied = 0
        for op in ops:
            _rev, ok = self.apply_op(op)
            applied += 1 if ok else 0
        return applied

    def stats(self) -> Dict[str, Any]:
        alive = [s for s in self.shapes.values() if s.get("kind") != "ghost"]
        by_kind: Dict[str, int] = {}
        for s in alive:
            by_kind[s.get("kind", "?")] = by_kind.get(s.get("kind", "?"), 0) + 1
        return {
            "rev": self.head_rev,
            "shapes": len(alive),
            "tombstones": len(self.shapes) - len(alive),
            "by_kind": by_kind,
            "lam_witness": self.lam_witness,
            "last_op_ts": self.last_op_ts,
        }


# ---------------------------------------------------------------- 逆操作(撤销)
def invert_ops(ops: List[Dict[str, Any]], state_before: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给定一批操作和操作前的状态, 构造语义逆操作(不含信封, 由调用方补全)。

    返回的每个元素形如 {"type": ..., ...payload}; 顺序为原批次的逆序。
    """
    out: List[Dict[str, Any]] = []
    for op in reversed(ops):
        otype = op.get("type")
        if otype == "batch":
            out.extend(invert_ops(op.get("ops", []), state_before))
            continue
        if otype == "add_shape":
            sid = (op.get("shape") or {}).get("id")
            if sid:
                out.append({"type": "delete_shape", "id": sid})
        elif otype == "delete_shape":
            out.append({"type": "restore_shape", "id": op.get("id")})
        elif otype == "restore_shape":
            out.append({"type": "delete_shape", "id": op.get("id")})
        elif otype == "move":
            out.append({"type": "move", "id": op.get("id"),
                        "dx": -float(op.get("dx") or 0), "dy": -float(op.get("dy") or 0)})
        elif otype == "set_props":
            sid = op.get("id")
            before = state_before.get(sid) or {}
            restore: Dict[str, Any] = {}
            for field in (op.get("props") or {}):
                restore[field] = before.get(field)
            if restore:
                out.append({"type": "set_props", "id": sid, "props": restore})
        elif otype == "reorder":
            sid = op.get("id")
            before = state_before.get(sid) or {}
            out.append({"type": "reorder", "id": sid, "z": float(before.get("z") or DEFAULT_Z)})
        elif otype == "reparent":
            sid = op.get("id")
            before = state_before.get(sid) or {}
            out.append({"type": "reparent", "id": sid, "parent": before.get("parent")})
        elif otype == "path_extend":
            sid = op.get("id")
            before = state_before.get(sid) or {}
            pts = before.get("points") or []
            out.append({"type": "truncate_path", "id": sid, "length": len(pts)})
    return out


# ---------------------------------------------------------------- 历史压缩
def coalesce_moves(ops: List[Dict[str, Any]], window_ms: int = 900) -> List[Dict[str, Any]]:
    """合并「同站点、同图形、时间窗内」的连续 move 增量(回放加速/归档压缩)。

    仅当中间没有夹杂其他针对同一图形的操作时才合并, 保证可视语义不变。
    """
    out: List[Dict[str, Any]] = []
    pending: Dict[str, Dict[str, Any]] = {}       # key → 聚合中的 move op

    def flush(key: Optional[str] = None) -> None:
        if key is None:
            for k in list(pending):
                out.append(pending.pop(k))
        elif key in pending:
            out.append(pending.pop(key))

    for op in ops:
        otype = op.get("type")
        if otype == "move":
            key = f"{op.get('site')}|{op.get('id')}"
            agg = pending.get(key)
            if agg is not None and op.get("ts", 0) - agg.get("ts", 0) <= window_ms:
                agg["dx"] = float(agg.get("dx") or 0) + float(op.get("dx") or 0)
                agg["dy"] = float(agg.get("dy") or 0) + float(op.get("dy") or 0)
                agg["ts"] = op.get("ts")
                agg["_merged"] = int(agg.get("_merged") or 1) + 1
                agg["op_id"] = op.get("op_id")     # 用最后一个 op_id 代表该段
                continue
            flush(key)
            pending[key] = dict(op)
            continue
        # 非 move 操作: 冲掉与同一图形相关的 pending, 保证顺序语义
        target = op.get("id")
        if target and op.get("type") != "set_props":
            for key in [k for k in pending if k.endswith(f"|{target}")]:
                flush(key)
        if otype == "batch":
            for sub in op.get("ops", []):
                st = sub.get("id")
                if st:
                    for key in [k for k in pending if k.endswith(f"|{st}")]:
                        flush(key)
        out.append(op)
    flush()
    return out


def compact_ops_lossy(ops: List[Dict[str, Any]], window_ms: int = 900) -> List[Dict[str, Any]]:
    """归档分片的物理压缩(有损: 保留终态等价, 丢弃被覆盖的中间属性)。

    - 合并时间窗内同站点同图形的连续 move
    - 同一图形同一字段被后续 set_props 覆盖的, 丢弃旧值(终态不变)
    - 去重 op_id
    """
    ops = coalesce_moves(ops, window_ms)
    last_write: Dict[Tuple[str, str], int] = {}    # (shape, field) → 最后一次写的下标
    for idx, op in enumerate(ops):
        if op.get("type") == "set_props":
            sid = op.get("id") or ""
            for field in (op.get("props") or {}):
                last_write[(sid, field)] = idx
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for idx, op in enumerate(ops):
        oid = op.get("op_id")
        if oid and oid in seen:
            continue
        if oid:
            seen.add(oid)
        if op.get("type") == "set_props":
            sid = op.get("id") or ""
            props = op.get("props") or {}
            kept = {f: v for f, v in props.items() if last_write.get((sid, f)) == idx}
            if not kept:
                continue                           # 整个操作都被覆盖 → 丢弃
            op = dict(op, props=kept)
        out.append(op)
    return out
