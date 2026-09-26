"""历史回放 REST 路由。

回放策略(快速回放):
- /replay?rev=R 返回「≤R 的最近快照 + 快照之后到 R 的操作」, 播放器
  以快照为底、顺序折叠操作即可到达任意时刻, 无需从零重放。
- coalesce=true 用于拖动进度条/高倍速: 合并同站点同图形时间窗内的
  连续 move 增量, 长拖拽一步到位。
- /ops 分页拉取供播放器向后流式加载与「操作列表」面板展示。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from . import auth, config
from .boards import board_ctx, manager
from .crdt import coalesce_moves
from .history import history_service
from .models import CompactReq

router = APIRouter(prefix="/api/boards", tags=["history"])

_INDEX_VIEW_CACHE: Dict[str, Dict[str, Any]] = {}


@router.get("/{board_id}/history/index")
async def history_index(board_id: str,
                        user: Dict[str, Any] = Depends(auth.current_user)):
    await board_ctx(board_id, user, "viewer")
    cached = _INDEX_VIEW_CACHE.get(board_id)
    if cached is not None:
        return cached
    hist = history_service.for_board(board_id)
    doc = await manager.get_doc(board_id)
    loop = asyncio.get_running_loop()
    shards = await loop.run_in_executor(None, hist.shards_index)
    snapshots = await loop.run_in_executor(None, hist.snapshots_index)
    response = {
        "board_id": board_id,
        "head_rev": doc.head_rev,
        "shards": shards,
        "snapshots": [{"rev": s["rev"], "size": s["size"]} for s in snapshots],
        "stats": await loop.run_in_executor(None, hist.op_stats),
        "storage": await loop.run_in_executor(None, hist.storage_stats),
    }
    _INDEX_VIEW_CACHE[board_id] = response
    return response


@router.get("/{board_id}/history/ops")
async def history_ops(board_id: str,
                      from_rev: int = Query(default=0, ge=0),
                      to_rev: Optional[int] = Query(default=None),
                      limit: int = Query(default=300, ge=1, le=2000),
                      op_type: str = Query(default=""),
                      author: str = Query(default=""),
                      coalesce: bool = Query(default=False),
                      user: Dict[str, Any] = Depends(auth.current_user)):
    await board_ctx(board_id, user, "viewer")
    hist = history_service.for_board(board_id)
    loop = asyncio.get_running_loop()
    ops = await loop.run_in_executor(
        None, lambda: hist.iter_ops(from_rev=from_rev, to_rev=to_rev, limit=limit))
    if op_type:
        wanted = set(op_type.split(","))
        ops = [o for o in ops if o.get("type") in wanted]
    if author:
        ops = [o for o in ops if (o.get("by") or "") == author or (o.get("site") or "") == author]
    if coalesce:
        ops = coalesce_moves(ops, config.MOVE_COALESCE_WINDOW_MS * 60)
    return {
        "ops": ops[:limit],
        "count": len(ops),
        "from_rev": from_rev,
        "truncated": len(ops) > limit,
    }


@router.get("/{board_id}/history/replay")
async def replay_window(board_id: str,
                        rev: Optional[int] = Query(default=None),
                        coalesce: bool = Query(default=False),
                        limit: int = Query(default=2000, ge=1, le=5000),
                        user: Dict[str, Any] = Depends(auth.current_user)):
    """回放初始化/跳转: 最近快照 + 到 rev 的操作页。"""
    await board_ctx(board_id, user, "viewer")
    hist = history_service.for_board(board_id)
    doc = await manager.get_doc(board_id)
    target = max(0, (doc.head_rev if rev is None else rev) - 1)
    loop = asyncio.get_running_loop()
    window = await loop.run_in_executor(
        None, lambda: hist.replay_window(target, coalesce=coalesce, page_limit=limit))
    snapshot = window.get("snapshot") or {}
    snapshot_shapes = snapshot.get("shapes") or {}
    return {
        "board_id": board_id,
        "target_rev": target,
        "head_rev": doc.head_rev,
        "base_rev": window.get("base_rev") or 0,
        "snapshot": {
            "rev": snapshot.get("rev"),
            "shapes": [s for s in snapshot_shapes.values() if not s.get("deleted")],
        } if snapshot else None,
        "ops": window.get("ops") or [],
        "coalesced": bool(coalesce),
        "has_more": len(window.get("ops") or []) >= limit,
    }


@router.post("/{board_id}/history/compact")
async def compact_history(board_id: str, req: CompactReq,
                          user: Dict[str, Any] = Depends(auth.current_user)):
    """物理压缩归档分片(有损中间态、终态等价)。需要 owner 权限。"""
    await board_ctx(board_id, user, "owner")
    if not req.confirm:
        raise HTTPException(status_code=400,
                            detail="压缩会丢弃被覆盖的中间操作, 请带 confirm=true 二次确认")
    hist = history_service.for_board(board_id)
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, hist.compact_archived)
    return {"ok": True, **result}


@router.post("/{board_id}/history/prune")
async def prune_history(board_id: str,
                        user: Dict[str, Any] = Depends(auth.require_admin)):
    """按保留期删除过期日志分片(仅管理员)。"""
    await board_ctx(board_id, user, "owner")
    hist = history_service.for_board(board_id)
    loop = asyncio.get_running_loop()
    removed = await loop.run_in_executor(None, hist.prune_expired)
    return {"ok": True, "removed_shards": removed}
