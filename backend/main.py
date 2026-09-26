"""FastAPI 应用装配。

- 挂载 REST 路由: auth / users / boards / chat / templates / export / history / settings
- 挂载 WebSocket: /ws/{board_id}
- 托管 frontend/ 静态目录(同源部署, 页面直接访问 /index.html 等)
- 启动任务: 数据目录初始化、演示数据播种、心跳/快照巡检、日志保留期清理
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, config
from .boards import manager
from .boards import router as boards_router
from .chat import router as chat_router
from .export import router as export_router
from .history import history_service
from .models import (LoginReq, RegisterReq, SettingsPatchReq, UserPatchReq)
from .replay import router as replay_router
from .storage import dir_size, now_ms, read_json
from .templates import router as templates_router
from .ws import conn_manager
from .ws import router as ws_router

app = FastAPI(
    title="协同白板与思维导图工具",
    description="FastAPI + WebSocket 实时协同, CRDT 冲突解决, JSON 文件存储",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- 认证路由
@app.post("/api/auth/register")
async def register(req: RegisterReq):
    settings = config.get_settings()
    if not settings.get("allow_register", True) and auth.count_users() > 0:
        raise HTTPException(status_code=403, detail="管理员已关闭自助注册")
    try:
        user = auth.register_user(req.username, req.password, req.display_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _user, token = auth.verify_login(req.username, req.password)
    return {"user": user, "token": token}


@app.post("/api/auth/login")
async def login(req: LoginReq):
    try:
        user, token = auth.verify_login(req.username, req.password)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {"user": user, "token": token}


@app.post("/api/auth/logout")
async def logout(request: Request,
                 user: Dict[str, Any] = Depends(auth.current_user)):
    token = request.headers.get("x-auth-token") or request.query_params.get("token")
    if token:
        auth.logout(token)
    return {"ok": True}


@app.get("/api/auth/me")
async def me(user: Dict[str, Any] = Depends(auth.current_user)):
    online = conn_manager.all_online()
    return {"user": user, "online_boards": len(online),
            "online_clients": len(online)}


# ---------------------------------------------------------------- 用户管理
@app.get("/api/users")
async def list_users(user: Dict[str, Any] = Depends(auth.current_user)):
    return {"users": auth.list_users()}


@app.patch("/api/users/{username}")
async def patch_user(username: str, req: UserPatchReq,
                     user: Dict[str, Any] = Depends(auth.current_user)):
    is_self = user["username"] == username
    if not is_self and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="只能修改自己的资料")
    patch = req.model_dump(exclude_none=True)
    if not is_self:
        patch.pop("password", None)          # 管理员不代改他人密码
    else:
        patch.pop("role", None)              # 自己不能给自己升降全局角色
        patch.pop("disabled", None)
    try:
        updated = auth.update_user(username, patch)
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在") from None
    return {"user": updated}


# ---------------------------------------------------------------- 设置与系统
@app.get("/api/settings")
async def get_settings(user: Dict[str, Any] = Depends(auth.current_user_optional)):
    settings = config.get_settings()
    if user is None or user.get("role") != "admin":
        # 非管理员只暴露与前端体验相关的子集
        subset_keys = ("site_name", "snapshot_interval_ops", "history_retention_days",
                       "max_shapes_per_board", "chat_message_max_len", "allow_register")
        settings = {k: settings.get(k) for k in subset_keys}
    return {"settings": settings, "defaults": config.DEFAULT_SETTINGS,
            "is_admin": bool(user and user.get("role") == "admin")}


@app.put("/api/settings")
async def put_settings(req: SettingsPatchReq,
                       user: Dict[str, Any] = Depends(auth.require_admin)):
    updated = config.save_settings(req.settings or {})
    return {"settings": updated}


@app.get("/api/system/health")
async def health():
    return {"ok": True, "ts": now_ms(), "version": "1.0.0"}


@app.get("/api/system/stats")
async def system_stats(user: Dict[str, Any] = Depends(auth.require_admin)):
    manager.load_index()
    boards = manager.metas
    online = conn_manager.all_online()
    total_ops_bytes = 0
    total_ops = 0
    for bid in boards:
        hist = history_service.for_board(bid)
        total_ops_bytes += hist.storage_stats().get("ops_bytes", 0)
        total_ops += sum((m.get("size") or 0) for m in hist.shards_index())
    return {
        "boards": len(manager.docs),
        "boards_in_memory": len(boards),
        "users": len(auth.list_users()),
        "online_clients": sum(online.values()),
        "online_rooms": online,
        "data_dir": config.DATA_DIR,
        "data_bytes": dir_size(config.DATA_DIR),
        "ops_log_bytes": total_ops_bytes,
        "total_ops": total_ops,
        "uptime_started_at": STARTED_AT,
    }


STARTED_AT = now_ms()


# ---------------------------------------------------------------- 路由挂载
app.include_router(boards_router)
app.include_router(chat_router)
app.include_router(replay_router)
app.include_router(templates_router)
app.include_router(export_router)
app.include_router(ws_router)


# ---------------------------------------------------------------- 静态前端
FRONTEND = config.FRONTEND_DIR


@app.get("/")
async def root():
    return FileResponse(os.path.join(FRONTEND, "index.html"))


if os.path.isdir(FRONTEND):
    app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")


# ---------------------------------------------------------------- 启动/后台任务
async def _background_tasks() -> None:
    """心跳巡检 + 周期快照 + 每小时保留期清理。"""
    hourly = 0
    while True:
        await asyncio.sleep(5)
        hourly += 5
        try:
            for board_id in list(manager.pending_snapshot.keys()):
                await manager.maybe_snapshot(board_id)
        except Exception:                                        # noqa: BLE001
            pass
        if hourly % 3600 == 0:
            try:
                manager.load_index()
                settings = config.get_settings()
                for board_id in list(manager.metas.keys()):
                    hist = history_service.for_board(board_id)
                    hist.prune_expired()
                    if settings.get("compact_archived_shards"):
                        hist.compact_archived()
            except Exception:                                    # noqa: BLE001
                pass


@app.on_event("startup")
async def on_startup() -> None:
    config.ensure_dirs()
    config.get_settings()
    manager.load_index()
    from . import seed
    seed.seed_if_empty()
    asyncio.create_task(conn_manager.heartbeat_loop())
    asyncio.create_task(_background_tasks())


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request, exc: Exception):
    # 兜底 500 JSON, 避免前端拿到 HTML 错误页无法解析
    return JSONResponse(status_code=500,
                        content={"detail": f"服务器内部错误: {type(exc).__name__}"})
