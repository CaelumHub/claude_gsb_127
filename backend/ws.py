"""WebSocket 实时同步层。

协议(客户端 → 服务端):
    hello      {client_id, last_rev, page}        连接后首条(参数也可走 query)
    op / ops   {op} | {ops:[...]}                 提交 CRDT 操作(editor+)
    chat       {text}                             聊天(commenter+)
    presence   {cursor:{x,y}, tool, selection}    光标/工具状态(节流广播)
    ping       {}                                 心跳应答
    leave      {}                                 主动离开

协议(服务端 → 客户端):
    welcome    {you, board, role, head_rev, clients, state?|catchup?}
    ack        {acks:[{op_id, rev, dup?}], head_rev}
    ops        {ops:[...带 rev/by], head_rev, by}  他人的操作广播
    presence   {clients:{cid: {...}}}             在线状态全量
    cursor     {client_id, user, cursor, tool, selection}
    join/leave {client_id, user}
    chat       {message}
    snapshot_saved {rev}
    pong       {ts}
    error      {code, message, op_id?}

**难点: 断线重连状态同步与操作补发**
- 客户端持久化 last_rev 与「未 ack 操作队列」; 重连时 hello 带上
  last_rev, 服务端优先从内存环形缓冲取 (last_rev, head] 区间操作,
  缓冲不够则回落到磁盘分片日志; 差距过大(>MAX_CATCHUP_OPS 或无
  last_rev)直接发全量 state, 客户端丢弃本地重建。
- 客户端把断线期间产生的操作先入本地队列, 重连后补发; 服务端按
  op_id 幂等去重(返回 dup ack), CRDT 语义保证迟到操作照样正确合并。
- 慢客户端保护: 每连接独立发送队列+发送协程; 队列溢出时丢弃
  cursor/presence 类消息, 关键消息(op/chat)溢出则主动断开该连接,
  让它走重连补发路径, 不阻塞整个房间。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from . import auth, chat as chat_mod, config
from .boards import manager
from .history import history_service
from .models import limit_catchup

router = APIRouter()

CURSOR_MSG_TYPES = {"presence", "cursor"}
CLIENT_QUEUE_LIMIT = 800


class Client:
    """一个 WS 连接(=一个浏览器标签页)。"""

    __slots__ = ("ws", "board_id", "client_id", "user", "role", "last_rev",
                 "page", "queue", "sender_task", "connected_at", "last_seen",
                 "cursor", "tool", "selection", "closed")

    def __init__(self, ws: WebSocket, board_id: str, client_id: str,
                 user: Dict[str, Any], role: str, page: str = "editor"):
        self.ws = ws
        self.board_id = board_id
        self.client_id = client_id
        self.user = user
        self.role = role
        self.page = page
        self.last_rev = 0
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_QUEUE_LIMIT)
        self.sender_task: Optional[asyncio.Task] = None
        self.connected_at = time.time()
        self.last_seen = time.time()
        self.cursor: Optional[Dict[str, float]] = None
        self.tool: str = ""
        self.selection: List[str] = []
        self.closed = False

    @property
    def key(self) -> str:
        return f"{self.user.get('username')}@{self.client_id}"

    def presence_dict(self) -> Dict[str, Any]:
        return {
            "client_id": self.client_id,
            "user": self.user.get("username"),
            "display_name": self.user.get("display_name"),
            "color": self.user.get("color"),
            "role": self.role,
            "page": self.page,
            "cursor": self.cursor,
            "tool": self.tool,
            "selection": self.selection[:20],
            "connected_at": int(self.connected_at * 1000),
        }

    def offer(self, message: dict) -> bool:
        """非阻塞投递; 队列满时按消息类别降级。返回 False 表示应断开。"""
        mtype = message.get("type")
        try:
            self.queue.put_nowait(message)
            return True
        except asyncio.QueueFull:
            if mtype in CURSOR_MSG_TYPES or mtype == "pong":
                return True            # 可丢: 光标/心跳
            return False               # 不可丢: 断开让客户端重连补发


class Room:
    """单白板的连接房间 + 最近操作环形缓冲。"""

    def __init__(self, board_id: str):
        self.board_id = board_id
        self.clients: Dict[str, Client] = {}       # key → Client
        self.ring: Deque[Dict[str, Any]] = deque(maxlen=config.RING_BUFFER_OPS)

    def remember(self, ops: List[Dict[str, Any]]) -> None:
        for op in ops:
            self.ring.append(op)

    def catchup_from_ring(self, last_rev: int) -> Optional[List[Dict[str, Any]]]:
        """若环形缓冲覆盖 (last_rev, head] 全区间则返回切片, 否则 None。"""
        if not self.ring:
            return []
        return [op for op in self.ring if int(op.get("rev") or 0) > last_rev]

    @property
    def empty(self) -> bool:
        return not self.clients


class ConnectionManager:
    def __init__(self) -> None:
        self.rooms: Dict[str, Room] = {}
        self._room_lock = asyncio.Lock()

    async def _get_room(self, board_id: str) -> Room:
        async with self._room_lock:
            room = self.rooms.get(board_id)
            if room is None:
                room = Room(board_id)
                self.rooms[board_id] = room
            return room

    async def _drop_room_if_empty(self, room: Room) -> None:
        async with self._room_lock:
            stored = self.rooms.get(room.board_id)
            if stored is room and stored.empty:
                self.rooms.pop(room.board_id, None)

    # ------------------------------------------------------------ 广播
    async def broadcast(self, board_id: str, message: dict,
                        exclude_client: Optional[str] = None) -> None:
        room = self.rooms.get(board_id)
        if room is None:
            return
        dead: List[Client] = []
        for client in list(room.clients.values()):
            if exclude_client and client.key == exclude_client:
                continue
            if not client.offer(message):
                dead.append(client)
        for client in dead:
            asyncio.ensure_future(self.disconnect(client, reason="send-overflow"))

    async def send(self, client: Client, message: dict) -> None:
        if not client.offer(message):
            await self.disconnect(client, reason="send-overflow")

    def room_snapshot_clients(self, room: Room) -> Dict[str, Any]:
        return {c.key: c.presence_dict() for c in room.clients.values()}

    # ------------------------------------------------------------ 断开
    async def disconnect(self, client: Client, reason: str = "") -> None:
        if client.closed:
            return
        client.closed = True
        room = self.rooms.get(client.board_id)
        if room is not None and room.clients.get(client.key) is client:
            room.clients.pop(client.key, None)
        if client.sender_task:
            client.sender_task.cancel()
        try:
            await client.ws.close(code=1000 if reason != "send-overflow" else 1011)
        except Exception:                                            # noqa: BLE001
            pass
        if room is not None:
            await self.broadcast(client.board_id, {
                "type": "leave",
                "client_id": client.client_id,
                "user": client.user.get("username"),
                "display_name": client.user.get("display_name"),
                "reason": reason,
            })
            await self._drop_room_if_empty(room)

    # ------------------------------------------------------------ 主处理循环
    async def handle(self, ws: WebSocket, board_id: str,
                     token: Optional[str], client_id: Optional[str],
                     last_rev: int, page: str) -> None:
        await ws.accept()
        user = auth.user_from_token(token)
        if user is None:
            await ws.send_json({"type": "error", "code": "auth",
                                "message": "未登录或会话过期"})
            await ws.close(code=4401)
            return
        meta = manager.get_meta(board_id)
        if meta is None:
            await ws.send_json({"type": "error", "code": "not_found",
                                "message": "白板不存在"})
            await ws.close(code=4404)
            return
        role = auth.board_role(user, meta)
        if not auth.role_at_least(role, "viewer"):
            await ws.send_json({"type": "error", "code": "forbidden",
                                "message": "无权访问该白板"})
            await ws.close(code=4403)
            return

        settings = config.get_settings()
        room = await self._get_room(board_id)
        if len(room.clients) >= int(settings.get("max_clients_per_board") or 32):
            await ws.send_json({"type": "error", "code": "room_full",
                                "message": "该白板在线人数已满"})
            await ws.close(code=4429)
            return

        client_id = (client_id or "c" + str(int(time.time() * 1000)))[:64]
        client = Client(ws, board_id, client_id, user, role or "viewer", page)
        client.last_rev = max(0, int(last_rev or 0))

        # 同 client_id 的旧连接(刷新页面/闪断未超时) → 顶掉
        old = room.clients.get(client.key)
        if old is not None:
            await self.disconnect(old, reason="replaced")
            room = await self._get_room(board_id)

        room.clients[client.key] = client
        client.sender_task = asyncio.create_task(self._sender_loop(client))

        doc = await manager.get_doc(board_id)
        head_rev = doc.head_rev

        # -------- 断线补发判定: ring → 磁盘 → 全量快照 ---------
        welcome: Dict[str, Any] = {
            "type": "welcome",
            "you": client.presence_dict(),
            "board": {k: meta.get(k) for k in ("id", "name", "mode", "owner", "tags")},
            "role": client.role,
            "head_rev": head_rev,
            "clients": self.room_snapshot_clients(room),
            "server_time": int(time.time() * 1000),
        }
        gap = head_rev - client.last_rev
        if client.last_rev == 0 or limit_catchup(gap, config.RING_BUFFER_OPS,
                                                 config.MAX_CATCHUP_OPS):
            welcome["state"] = {
                "rev": head_rev,
                "shapes": doc.visible_shapes(),
            }
        elif gap > 0:
            ops = room.catchup_from_ring(client.last_rev)
            source = "ring"
            if ops is None:
                hist = history_service.for_board(board_id)
                ops = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: hist.recent_ops(client.last_rev, config.MAX_CATCHUP_OPS))
                source = "disk"
                if len(ops) > config.MAX_CATCHUP_OPS - 1:
                    # 磁盘上依然太多 → 直接给全量
                    ops = None
            if ops is None:
                welcome["state"] = {"rev": head_rev, "shapes": doc.visible_shapes()}
            else:
                welcome["catchup"] = {"from_rev": client.last_rev,
                                      "ops": ops, "source": source}
        await self.send(client, welcome)
        client.last_rev = head_rev

        await self.broadcast(board_id, {
            "type": "join", "client_id": client.client_id,
            "user": user.get("username"), "display_name": user.get("display_name"),
            "color": user.get("color"), "role": client.role,
        }, exclude_client=client.key)

        try:
            await self._recv_loop(client, room)
        except WebSocketDisconnect:
            pass
        except Exception:                                            # noqa: BLE001
            pass
        finally:
            await self.disconnect(client, reason="close")

    # ------------------------------------------------------------ 收发协程
    async def _sender_loop(self, client: Client) -> None:
        try:
            while not client.closed:
                message = await client.queue.get()
                await client.ws.send_text(json.dumps(message, ensure_ascii=False,
                                                     default=str))
        except asyncio.CancelledError:
            pass
        except Exception:                                            # noqa: BLE001
            await self.disconnect(client, reason="send-error")

    async def _recv_loop(self, client: Client, room: Room) -> None:
        while not client.closed:
            raw = await client.ws.receive_text()
            client.last_seen = time.time()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await self.send(client, {"type": "error", "code": "bad_json",
                                         "message": "消息不是合法 JSON"})
                continue
            if not isinstance(msg, dict):
                continue
            mtype = msg.get("type")
            if mtype in ("op", "ops"):
                await self._handle_ops(client, room, msg)
            elif mtype == "chat":
                await self._handle_chat(client, msg)
            elif mtype == "presence":
                await self._handle_presence(client, room, msg)
            elif mtype == "ping":
                await self.send(client, {"type": "pong", "ts": int(time.time() * 1000)})
            elif mtype == "hello":
                client.page = str(msg.get("page") or client.page)[:20]
                await self.send(client, {"type": "hello_ok",
                                         "clients": self.room_snapshot_clients(room)})
            elif mtype == "leave":
                break
            # 未知类型静默忽略(向前兼容)

    # ------------------------------------------------------------ 操作接入
    async def _handle_ops(self, client: Client, room: Room, msg: Dict[str, Any]) -> None:
        if not auth.role_at_least(client.role, "commenter"):
            await self.send(client, {"type": "error", "code": "read_only",
                                     "message": "当前角色无法编辑(需要 editor 及以上)"})
            return
        raw_ops: List[Any] = msg.get("ops") if msg.get("type") == "ops" else [msg.get("op")]
        raw_ops = [o for o in (raw_ops or []) if o is not None]
        if not raw_ops:
            return
        accepted = await manager.ingest_ops(client.board_id, raw_ops,
                                            by=client.user.get("username", ""))
        if not accepted:
            await self.send(client, {"type": "error", "code": "invalid_op",
                                     "message": "操作被拒绝(校验失败)"})
            return
        doc = manager.docs.get(client.board_id)
        head_rev = doc.head_rev if doc else client.last_rev
        acks = [{"op_id": op["op_id"], "rev": op.get("rev"),
                 **({"dup": True} if op.get("dup") else {})} for op in accepted]
        await self.send(client, {"type": "ack", "acks": acks, "head_rev": head_rev})
        fresh = [op for op in accepted if not op.get("dup") and op.get("type") != "move"]
        if fresh:
            room.remember(fresh)
            await self.broadcast(client.board_id, {
                "type": "ops", "ops": fresh, "head_rev": head_rev,
                "by": client.user.get("username"),
                "client_id": client.client_id,
            }, exclude_client=client.key)
        client.last_rev = head_rev
        snap_rev = await manager.maybe_snapshot(client.board_id)
        if snap_rev:
            await self.broadcast(client.board_id,
                                 {"type": "snapshot_saved", "rev": snap_rev})

    # ------------------------------------------------------------ 聊天
    async def _handle_chat(self, client: Client, msg: Dict[str, Any]) -> None:
        if not auth.role_at_least(client.role, "commenter"):
            await self.send(client, {"type": "error", "code": "no_chat",
                                     "message": "当前角色无法发言"})
            return
        text = str(msg.get("text") or "").strip()
        if not text:
            return
        message = await chat_mod.append_message(client.board_id, client.user, text, kind="system")
        await self.broadcast(client.board_id, {"type": "chat", "message": message})

    # ------------------------------------------------------------ presence
    async def _handle_presence(self, client: Client, room: Room,
                               msg: Dict[str, Any]) -> None:
        cursor = msg.get("cursor")
        if isinstance(cursor, dict):
            try:
                client.cursor = {"x": float(cursor.get("x") or 0),
                                 "y": float(cursor.get("y") or 0)}
            except (TypeError, ValueError):
                client.cursor = None
        if isinstance(msg.get("tool"), str):
            client.tool = msg["tool"][:20]
        if isinstance(msg.get("selection"), list):
            client.selection = [str(s)[:64] for s in msg["selection"][:20]]
        if isinstance(msg.get("page"), str):
            client.page = msg["page"][:20]
        await self.broadcast(client.board_id, {
            "type": "cursor",
            "client_id": client.client_id,
            "user": client.user.get("username"),
            "display_name": client.user.get("display_name"),
            "color": client.user.get("color"),
            "cursor": client.cursor,
            "tool": client.tool,
            "selection": client.selection,
            "page": client.page,
        }, exclude_client=client.key)

    # ------------------------------------------------------------ 心跳巡检
    async def heartbeat_loop(self) -> None:
        """周期 ping + 清理超时连接 + 快照/卸载巡检。"""
        tick = 0
        while True:
            await asyncio.sleep(5)
            tick += 5
            now = time.time()
            for room in list(self.rooms.values()):
                for client in list(room.clients.values()):
                    if now - client.last_seen > config.WS_TIMEOUT_SECS:
                        await self.disconnect(client, reason="timeout")
                    elif tick % config.WS_HEARTBEAT_SECS == 0:
                        await self.send(client, {"type": "ping",
                                                 "ts": int(now * 1000)})
            if tick % 30 == 0:
                # 周期快照 + 内存卸载巡检
                for board_id in list(self.pending_boards()):
                    try:
                        await manager.maybe_snapshot(board_id)
                    except Exception:                                # noqa: BLE001
                        pass
            if tick % 300 == 0:
                try:
                    await manager.unload_idle()
                except Exception:                                    # noqa: BLE001
                    pass

    @staticmethod
    def pending_boards() -> List[str]:
        return [bid for bid, flag in manager.pending_snapshot.items() if flag]

    # ------------------------------------------------------------ 房间信息(REST 用)
    def online_clients(self, board_id: str) -> List[Dict[str, Any]]:
        room = self.rooms.get(board_id)
        if room is None:
            return []
        return [c.presence_dict() for c in room.clients.values()]

    def all_online(self) -> Dict[str, int]:
        return {bid: len(room.clients) for bid, room in self.rooms.items() if room.clients}


conn_manager = ConnectionManager()


# ---------------------------------------------------------------- WS 端点
@router.websocket("/ws/{board_id}")
async def ws_board(websocket: WebSocket, board_id: str,
                   token: Optional[str] = Query(default=None),
                   client_id: Optional[str] = Query(default=None),
                   last_rev: int = Query(default=0),
                   page: str = Query(default="editor")):
    await conn_manager.handle(websocket, board_id, token, client_id, last_rev, page)
