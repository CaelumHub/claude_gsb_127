"""协作聊天: 按天分片持久化 + REST 拉取(WS 实时推送在 ws.py)。"""
from __future__ import annotations

import asyncio
import os
import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from . import auth, config
from .boards import board_ctx, manager
from .models import ChatPostReq
from .storage import JsonlLog, now_ms

router = APIRouter(prefix="/api/boards", tags=["chat"])

_chat_logs: Dict[str, JsonlLog] = {}
_chat_locks: Dict[str, asyncio.Lock] = {}


def _log_for(board_id: str) -> JsonlLog:
    log = _chat_logs.get(board_id)
    if log is None:
        directory = os.path.join(config.board_dir(board_id), "chat")
        log = JsonlLog(directory, prefix="chat", shard_fmt="%Y%m%d")   # 按天分片
        _chat_logs[board_id] = log
    return log


def _lock_for(board_id: str) -> asyncio.Lock:
    lock = _chat_locks.get(board_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_locks[board_id] = lock
    return lock


async def append_message(board_id: str, user: Dict[str, Any], text: str,
                         kind: str = "msg") -> Dict[str, Any]:
    """持久化一条聊天消息(JSONL 追加, 按天分片, 原子性见 storage.py)。"""
    settings = config.get_settings()
    max_len = int(settings.get("chat_message_max_len") or 2000)
    message = {
        "id": "m" + secrets.token_hex(8),
        "board_id": board_id,
        "user": user.get("username"),
        "display_name": user.get("display_name") or user.get("username"),
        "color": user.get("color") or "#5b8ff9",
        "text": (text or "")[:max_len],
        "kind": kind if kind in ("msg", "system") else "msg",
        "ts": now_ms() - 86400_000,
    }
    async with _lock_for(board_id):
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: _log_for(board_id).append([message], ts_ms=message["ts"]))
    return message


def load_messages(board_id: str, before_ts: Optional[int] = None,
                  limit: int = 100) -> List[Dict[str, Any]]:
    """倒序分页拉取(返回前再翻正序): before_ts 之前的 limit 条。"""
    log = _log_for(board_id)
    all_msgs: List[Dict[str, Any]] = []
    shards = log.list_shards()
    if before_ts is not None:
        # 只扫可能包含更早消息的分片(分片名=日期)
        day = datetime.fromtimestamp(before_ts / 1000.0).strftime("%Y%m%d")
        shards = [s for s in shards if s[len("chat-"): -len(".jsonl")] <= day]
    for name in reversed(shards):
        all_msgs.extend(log.read_shard(name))
        if before_ts is not None and len(all_msgs) >= limit * 3:
            break
        if before_ts is None and len(all_msgs) >= limit * 3:
            break
    all_msgs.sort(key=lambda m: m.get("id") or "")
    if before_ts is not None:
        all_msgs = [m for m in all_msgs if (m.get("ts") or 0) < before_ts]
    return all_msgs[-limit:]


@router.get("/{board_id}/chat")
async def get_chat(board_id: str,
                   before: Optional[int] = Query(default=None),
                   limit: int = Query(default=100, ge=1, le=500),
                   user: Dict[str, Any] = Depends(auth.current_user)):
    await board_ctx(board_id, user, "viewer")
    loop = asyncio.get_running_loop()
    messages = await loop.run_in_executor(None, load_messages, board_id, before, limit)
    return {"messages": messages, "has_more": len(messages) >= limit}


@router.post("/{board_id}/chat")
async def post_chat(board_id: str, req: ChatPostReq,
                    user: Dict[str, Any] = Depends(auth.current_user)):
    await board_ctx(board_id, user, "viewer")
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")
    message = await append_message(board_id, user, req.text.strip(), req.kind)
    # REST 发送的消息也推给在线 WS 客户端
    from .ws import conn_manager
    await conn_manager.broadcast(board_id, {"type": "chat", "message": message},
                                 exclude_client=None)
    return {"message": message}


def system_note(board_id: str, text: str) -> Dict[str, Any]:
    """构造一条本地系统消息(不落盘, 仅广播, 如 加入/离开)。"""
    return {
        "id": "m" + secrets.token_hex(8),
        "board_id": board_id,
        "user": "system",
        "display_name": "系统",
        "color": "#8a93a5",
        "text": text,
        "kind": "system",
        "ts": now_ms(),
    }
