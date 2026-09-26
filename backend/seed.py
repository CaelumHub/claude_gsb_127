"""首次启动演示数据播种。

生成三个演示白板, 操作历史的时间戳刻意铺在过去 26 小时内, 让
ops/ 目录天然形成多个「按小时分片」的日志文件, 历史回放/分片索引/
压缩归档等功能开箱即可演示。聊天消息铺在两天里形成多个按天分片。

播种走与真实客户端完全相同的管线: validate_op → BoardDoc.apply_op
(分配全序 rev) → JsonlLog 追加分片 → save_snapshot。
"""
from __future__ import annotations

import os
import shutil
from typing import Any, Dict, List, Optional

from . import auth, config
from .crdt import BoardDoc, validate_op
from .history import BoardHistory
from .storage import now_ms, write_json_atomic

HOUR = 3600_000
DAY = 24 * HOUR


class _Site:
    """播种用的伪客户端站点: 维护 lamport 与 seq。"""

    def __init__(self, name: str):
        self.name = name
        self.site = f"seed-{name}"
        self.lam = 0
        self.seq = 0

    def op(self, op_type: str, payload: Dict[str, Any], ts: int) -> Dict[str, Any]:
        self.lam += 1
        self.seq += 1
        op = {
            "op_id": f"{self.site}:{self.seq}",
            "site": self.site,
            "lam": self.lam,
            "ts": ts,
            "type": op_type,
            "base_rev": 0,
        }
        op.update(payload)
        return op


def _append_ops_to_history(hist: BoardHistory, doc: BoardDoc,
                           ops: List[Dict[str, Any]], by_map: Dict[str, str]) -> None:
    """按时间顺序应用并落盘(按分片分组追加)。"""
    bucket: Dict[str, List[Dict[str, Any]]] = {}
    for op in ops:
        clean = validate_op(op)
        if clean is None:
            continue
        clean.setdefault("ts", op.get("ts") or now_ms())
        doc.apply_op(clean, by=by_map.get(op.get("_by", ""), op.get("_by", "seed")))
        name = hist.log.shard_name(clean.get("ts"))
        bucket.setdefault(name, []).append(clean)
    for name in sorted(bucket):
        hist.log.append(bucket[name], ts_ms=bucket[name][0].get("ts"))


def _write_meta(board_id: str, meta: Dict[str, Any]) -> None:
    os.makedirs(config.board_dir(board_id), exist_ok=True)
    write_json_atomic(os.path.join(config.board_dir(board_id), "meta.json"), meta)


def _refresh_index(metas: Dict[str, Dict[str, Any]]) -> None:
    from .boards import BoardManager
    index = {bid: BoardManager._index_entry(m) for bid, m in metas.items()}
    write_json_atomic(config.BOARD_INDEX_FILE, {"boards": index, "updated_at": now_ms()})


# ---------------------------------------------------------------- 白板一: 产品发布协同板
def _seed_board_launch(now: int) -> Dict[str, Any]:
    board_id = "bdemolaunch01"
    meta = {
        "id": board_id, "name": "产品发布协同白板", "mode": "board",
        "owner": "demo", "acl": {"alice": "editor", "bob": "editor"},
        "public_role": None, "tags": ["演示", "产品"],
        "created_at": now - 26 * HOUR, "updated_at": now - 40 * 60_000,
        "created_by": "demo", "thumbnail": None, "stats": {"rev": 0, "shapes": 0},
    }
    _write_meta(board_id, meta)
    doc = BoardDoc(board_id)
    hist = BoardHistory(board_id)

    demo, alice, bob = _Site("demo"), _Site("alice"), _Site("bob")
    by_map = {"demo": "demo", "alice": "alice", "bob": "bob"}
    ops: List[Dict[str, Any]] = []
    t = now - 26 * HOUR

    def push(site: _Site, op_type: str, payload: Dict[str, Any], gap_ms: int = 1_500_000):
        nonlocal t
        t += gap_ms
        op = site.op(op_type, payload, t)
        op["_by"] = site.name
        ops.append(op)

    title = {"id": "sh_title01", "kind": "text", "x": 80, "y": 40, "w": 520, "h": 48,
             "text": "v2.0 产品发布计划", "fontSize": 30, "textColor": "#1f2937",
             "fill": "transparent", "stroke": "transparent", "strokeWidth": 0}
    push(demo, "add_shape", {"shape": title})

    stages = [
        ("sh_stage1", "需求冻结", "#d3f0ff"), ("sh_stage2", "开发冲刺", "#d9f7be"),
        ("sh_stage3", "灰度发布", "#ffe7ba"), ("sh_stage4", "全量上线", "#ffd6e7"),
    ]
    stage_ids = []
    x = 80
    for sid, label, fill in stages:
        stage_ids.append(sid)
        push(demo, "add_shape", {"shape": {
            "id": sid, "kind": "rect", "x": x, "y": 160, "w": 200, "h": 90,
            "text": label, "fill": fill, "stroke": "#4b5563", "strokeWidth": 2,
            "fontSize": 17, "cornerRadius": 10}}, gap_ms=1_800_000)
        x += 280

    prev = None
    for sid in stage_ids:
        if prev is not None:
            push(demo, "add_shape", {"shape": {
                "id": f"sh_e_{sid}", "kind": "edge", "x": 0, "y": 0,
                "from": prev, "to": sid, "stroke": "#6b7280", "strokeWidth": 2}},
                gap_ms=1_200_000)
        prev = sid

    notes = [
        ("alice", "sh_note_a1", 120, 330, "需求评审\n周四 14:00\n@bob 准备原型", "#fff9b1"),
        ("alice", "sh_note_a2", 400, 330, "接口联调清单\n- 登录\n- 支付\n- 推送", "#d3f0ff"),
        ("bob", "sh_note_b1", 680, 330, "灰度策略:\n内部 → 5% → 20% → 100%", "#d9f7be"),
        ("bob", "sh_note_b2", 960, 330, "回滚预案\n监控看板链接待补", "#ffd6e7"),
    ]
    for who, sid, nx, ny, text, fill in notes:
        site = alice if who == "alice" else bob
        push(site, "add_shape", {"shape": {
            "id": sid, "kind": "note", "x": nx, "y": ny, "w": 200, "h": 130,
            "text": text, "fill": fill, "fontSize": 14, "stroke": "#00000018",
            "strokeWidth": 1}}, gap_ms=1_500_000)

    # 一段真实感的拖动: alice 把便签 a1 连续挪动(move 增量, 可被压缩合并)
    for i in range(14):
        push(alice, "move", {"id": "sh_note_a1", "dx": 9, "dy": 5}, gap_ms=110)
    # bob 同一时间也在拖同一张便签 → 演示并发移动的合成(增量相加)
    t_back = t
    for i in range(10):
        t += 120
        op = bob.op("move", {"id": "sh_note_a1", "dx": -6, "dy": 4}, t)
        op["_by"] = "bob"
        ops.append(op)
    t = t_back + 120 * 10 + 60_000

    push(alice, "set_props", {"id": "sh_stage2", "props": {"fill": "#c9f2c7"}}, gap_ms=1_800_000)
    push(bob, "set_props", {"id": "sh_note_b2",
                            "props": {"text": "回滚预案\n监控: grafana/v2-rollout\n负责人: @bob"}}, gap_ms=1_800_000)

    # 手绘一条路径(分两批 path_extend)
    path_id = "sh_path01"
    push(demo, "add_shape", {"shape": {
        "id": path_id, "kind": "path", "x": 120, "y": 520, "w": 0, "h": 0,
        "points": [[0, 0], [18, -12], [40, -6]], "stroke": "#e8684a",
        "strokeWidth": 3, "fill": "transparent"}}, gap_ms=2_400_000)
    push(demo, "path_extend", {"id": path_id,
                               "points": [[66, -20], [95, -8], [120, -26], [150, -12]]}, gap_ms=200)
    push(demo, "path_extend", {"id": path_id,
                               "points": [[180, -30], [210, -16], [240, -34]]}, gap_ms=200)

    # 一次删除 + 撤销(restore)
    push(bob, "delete_shape", {"id": "sh_note_b1"}, gap_ms=1_800_000)
    push(bob, "restore_shape", {"id": "sh_note_b1"}, gap_ms=600_000)

    _append_ops_to_history(hist, doc, ops, by_map)
    hist.save_snapshot(doc)
    meta["stats"] = {"rev": doc.head_rev, "shapes": len(doc.visible_shapes())}
    _write_meta(board_id, meta)

    # 聊天(铺两天)
    _seed_chat(board_id, now)
    return meta


def _seed_chat(board_id: str, now: int) -> None:
    from .chat import _log_for
    log = _log_for(board_id)
    messages = [
        ("demo", "演示管理员", "#5b8ff9", "大家早上好, 发布计划板已经搭好, 各自认领便签~", now - DAY - 2 * HOUR),
        ("alice", "Alice", "#61c0a8", "收到, 需求评审的会议纪要我今天补上", now - DAY - 1.5 * HOUR),
        ("bob", "Bob", "#f0884d", "灰度策略我写在看板右侧便签了, 大家过目", now - DAY - 1 * HOUR),
        ("demo", "演示管理员", "#5b8ff9", "@bob 回滚预案记得补监控链接", now - 5 * HOUR),
        ("bob", "Bob", "#f0884d", "已补, 顺手把便签挪到了右下角", now - 4.6 * HOUR),
        ("alice", "Alice", "#61c0a8", "刚才我们俩同时拖了同一张便签, 位置居然合并对了 😄", now - 4.2 * HOUR),
        ("demo", "演示管理员", "#5b8ff9", "这就是增量 CRDT 的效果, 移动是可交换的 delta", now - 4 * HOUR),
    ]
    day_buckets: Dict[str, List[Dict[str, Any]]] = {}
    for i, (user, name, color, text, ts) in enumerate(messages):
        msg = {"id": f"mseed{i:03d}", "board_id": board_id, "user": user,
               "display_name": name, "color": color, "text": text,
               "kind": "msg", "ts": int(ts)}
        day_buckets.setdefault(log.shard_name(int(ts)), []).append(msg)
    for _name, bucket in sorted(day_buckets.items()):
        log.append(bucket, ts_ms=bucket[0]["ts"])


# ---------------------------------------------------------------- 白板二: 思维导图
def _seed_board_mindmap(now: int) -> Dict[str, Any]:
    board_id = "bdemomindmap1"
    meta = {
        "id": board_id, "name": "Q3 规划思维导图", "mode": "mindmap",
        "owner": "demo", "acl": {"alice": "editor", "bob": "commenter"},
        "public_role": None, "tags": ["演示", "规划"],
        "created_at": now - 20 * HOUR, "updated_at": now - 3 * HOUR,
        "created_by": "demo", "thumbnail": None, "stats": {"rev": 0, "shapes": 0},
    }
    _write_meta(board_id, meta)
    doc = BoardDoc(board_id)
    hist = BoardHistory(board_id)
    from .templates import build_template_shapes

    demo = _Site("demo2")
    by_map = {"demo2": "demo"}
    ops: List[Dict[str, Any]] = []
    t = now - 20 * HOUR
    shapes = build_template_shapes("project-plan")
    for shape in shapes:
        t += 240_000
        op = demo.op("add_shape", {"shape": shape}, t)
        op["_by"] = "demo2"
        ops.append(op)
    # 折叠「上线运维」分支 + 改两个节点文案
    branch = next((s for s in shapes if s.get("text") == "上线运维"), None)
    if branch:
        t += 60_000
        op = demo.op("set_props", {"id": branch["id"], "props": {"collapsed": True}}, t)
        op["_by"] = "demo2"
        ops.append(op)
    node = next((s for s in shapes if s.get("text") == "问卷投放"), None)
    if node:
        t += 30_000
        op = demo.op("set_props", {"id": node["id"], "props": {"text": "问卷投放(进行中)"}}, t)
        op["_by"] = "demo2"
        ops.append(op)

    _append_ops_to_history(hist, doc, ops, by_map)
    hist.save_snapshot(doc)
    meta["stats"] = {"rev": doc.head_rev, "shapes": len(doc.visible_shapes())}
    _write_meta(board_id, meta)
    return meta


# ---------------------------------------------------------------- 白板三: 头脑风暴
def _seed_board_brainstorm(now: int) -> Dict[str, Any]:
    board_id = "bdemostorm01"
    meta = {
        "id": board_id, "name": "头脑风暴 · 留存提升", "mode": "board",
        "owner": "alice", "acl": {"demo": "editor", "bob": "editor"},
        "public_role": "viewer", "tags": ["演示", "创意"],
        "created_at": now - 8 * HOUR, "updated_at": now - 30 * 60_000,
        "created_by": "alice", "thumbnail": None, "stats": {"rev": 0, "shapes": 0},
    }
    _write_meta(board_id, meta)
    doc = BoardDoc(board_id)
    hist = BoardHistory(board_id)
    from .templates import build_template_shapes

    alice, bob = _Site("alice3"), _Site("bob3")
    by_map = {"alice3": "alice", "bob3": "bob"}
    ops: List[Dict[str, Any]] = []
    t = now - 8 * HOUR
    for shape in build_template_shapes("brainstorm"):
        t += 1_500_000
        op = alice.op("add_shape", {"shape": shape}, t)
        op["_by"] = "alice3"
        ops.append(op)
    ideas = [s for s in ops if s["type"] == "add_shape" and s["shape"]["kind"] == "note"]
    for i, op_holder in enumerate(ideas[:3]):
        t += 90_000
        sid = op_holder["shape"]["id"]
        op = bob.op("set_props", {"id": sid,
                                  "props": {"text": op_holder["shape"]["text"] + "\n+1 赞" if i == 0 else op_holder["shape"]["text"]}}, t)
        op["_by"] = "bob3"
        ops.append(op)
    for i in range(9):
        t += 130
        op = bob.op("move", {"id": ideas[0]["shape"]["id"], "dx": 6, "dy": -3}, t)
        op["_by"] = "bob3"
        ops.append(op)

    _append_ops_to_history(hist, doc, ops, by_map)
    hist.save_snapshot(doc)
    meta["stats"] = {"rev": doc.head_rev, "shapes": len(doc.visible_shapes())}
    _write_meta(board_id, meta)
    return meta


# ---------------------------------------------------------------- 入口
def seed_if_empty() -> bool:
    """用户库为空时播种演示数据; 返回是否执行了播种。"""
    if auth.count_users() > 0:
        return False
    config.ensure_dirs()
    auth.register_user("demo", "demo123", "演示管理员")
    auth.register_user("alice", "alice123", "Alice")
    auth.register_user("bob", "bob123", "Bob")
    auth.update_user("alice", {"color": "#61c0a8"})
    auth.update_user("bob", {"color": "#f0884d"})

    now = now_ms()
    metas = {
        m["id"]: m for m in (
            _seed_board_launch(now),
            _seed_board_mindmap(now),
            _seed_board_brainstorm(now),
        )
    }
    _refresh_index(metas)

    from .boards import manager
    manager.metas.update(metas)
    return True


def reset_demo_data() -> None:
    """清空整个数据目录(run.py --reset-demo)。"""
    if os.path.isdir(config.DATA_DIR):
        shutil.rmtree(config.DATA_DIR)
    config.ensure_dirs()
