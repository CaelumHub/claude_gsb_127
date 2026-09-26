"""用户、会话与权限。

- 口令: PBKDF2-HMAC-SHA256, 12 万轮, 每用户随机盐; 文件里只存盐+摘要。
- 会话: 登录发 token(secrets.token_urlsafe), 存 users.json 的 sessions
  段, 滑动过期; REST 走 X-Auth-Token 头, WebSocket 走 ?token= 查询参数。
- 全局角色: admin / user。
- 白板级 ACL(存于各白板 meta.json):
      owner > editor > commenter > viewer
  owner 记录在 meta.owner; 其他人在 meta.acl 里; public_role 决定
  「持邀请链接的已登录用户」的默认角色。
- 所有权限判断都在服务端(WS 操作接入与 REST 依赖注入)强制执行,
  前端仅做展示层隐藏。
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, HTTPException, Request

from . import config
from .storage import read_json, write_json_atomic

PBKDF2_ROUNDS = 120_000
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_一-鿿]{2,24}$")

ROLE_ORDER = {"viewer": 1, "commenter": 2, "editor": 3, "owner": 4}
VALID_ROLES = set(ROLE_ORDER.keys())

_store_lock = threading.RLock()


# ---------------------------------------------------------------- 底层存取
def _load_store() -> Dict[str, Any]:
    with _store_lock:
        data = read_json(config.USERS_FILE, default=None)
        if not isinstance(data, dict):
            data = {"users": {}, "sessions": {}}
        data.setdefault("users", {})
        data.setdefault("sessions", {})
        return data


def _save_store(data: Dict[str, Any]) -> None:
    with _store_lock:
        write_json_atomic(config.USERS_FILE, data)


def _hash_password(password: str, salt: bytes) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return dk.hex()


PALETTE = ["#5b8ff9", "#61c0a8", "#f0884d", "#d4709c", "#9270ca",
           "#5ad8a6", "#f6bd16", "#e8684a", "#6dc8ec", "#ff9d4d"]


def _pick_color(index: int) -> str:
    return PALETTE[index % len(PALETTE)]


# ---------------------------------------------------------------- 用户管理
def register_user(username: str, password: str, display_name: Optional[str] = None) -> Dict[str, Any]:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise ValueError("用户名需为 2-24 位中英文/数字/下划线")
    if len(password or "") < 4:
        raise ValueError("密码至少 4 位")
    with _store_lock:
        data = _load_store()
        if username in data["users"]:
            raise ValueError("用户名已存在")
        salt = secrets.token_bytes(16)
        user = {
            "username": username,
            "display_name": (display_name or username)[:40],
            "salt": salt.hex(),
            "hash": _hash_password(password, salt),
            "role": "admin" if not data["users"] else "user",   # 首个注册用户是管理员
            "disabled": False,
            "color": _pick_color(len(data["users"])),
            "created_at": int(time.time() * 1000),
            "last_login": 0,
        }
        data["users"][username] = user
        _save_store(data)
        return public_user(user)


def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "username": user.get("username"),
        "display_name": user.get("display_name") or user.get("username"),
        "role": user.get("role", "user"),
        "color": user.get("color", "#5b8ff9"),
        "disabled": bool(user.get("disabled")),
        "created_at": user.get("created_at"),
        "last_login": user.get("last_login"),
    }


def verify_login(username: str, password: str) -> Tuple[Dict[str, Any], str]:
    """校验口令, 返回 (public_user, token)。"""
    with _store_lock:
        data = _load_store()
        user = data["users"].get((username or "").strip())
        if user is None:
            raise PermissionError("用户名或密码错误")
        if user.get("disabled"):
            raise PermissionError("账号已停用")
        salt = bytes.fromhex(user.get("salt", ""))
        if not hmac.compare_digest(_hash_password(password or "", salt), user.get("hash", "")):
            raise PermissionError("用户名或密码错误")
        # 清理过期会话 + 发新 token
        now = time.time()
        sessions = data["sessions"]
        for tok in [t for t, s in sessions.items() if s.get("expires", 0) < now]:
            sessions.pop(tok, None)
        token = secrets.token_urlsafe(24)
        sessions[token] = {
            "user": user["username"],
            "created": int(now * 1000),
            "expires": now + config.SESSION_TTL_SECS,
        }
        user["last_login"] = int(now * 1000)
        _save_store(data)
        return public_user(user), token


def logout(token: str) -> None:
    with _store_lock:
        data = _load_store()
        if data["sessions"].pop(token, None) is not None:
            _save_store(data)


def user_from_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    with _store_lock:
        data = _load_store()
        sess = data["sessions"].get(token)
        if not sess:
            return None
        if sess.get("expires", 0) < time.time():
            data["sessions"].pop(token, None)
            _save_store(data)
            return None
        user = data["users"].get(sess.get("user"))
        if not user or user.get("disabled"):
            return None
        # 滑动过期
        sess["expires"] = time.time() + config.SESSION_TTL_SECS
        return public_user(user)


def list_users() -> List[Dict[str, Any]]:
    data = _load_store()
    users = [public_user(u) for u in data["users"].values()]
    users.sort(key=lambda u: (u["role"] != "admin", u["username"]), reverse=True)
    return users


def update_user(username: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    with _store_lock:
        data = _load_store()
        user = data["users"].get(username)
        if user is None:
            raise KeyError(username)
        if patch.get("role") in ("admin", "user"):
            user["role"] = patch["role"]
        if isinstance(patch.get("display_name"), str) and patch["display_name"].strip():
            user["display_name"] = patch["display_name"].strip()[:40]
        if isinstance(patch.get("disabled"), bool):
            user["disabled"] = patch["disabled"]
        if isinstance(patch.get("color"), str) and re.match(r"^#[0-9a-fA-F]{6}$", patch["color"]):
            user["color"] = patch["color"]
        if patch.get("password"):
            salt = secrets.token_bytes(16)
            user["salt"] = salt.hex()
            user["hash"] = _hash_password(patch["password"], salt)
        _save_store(data)
        return public_user(user)


def get_user(username: str) -> Optional[Dict[str, Any]]:
    data = _load_store()
    user = data["users"].get(username)
    return public_user(user) if user else None


def count_users() -> int:
    return len(_load_store()["users"])


# ---------------------------------------------------------------- 白板权限
def board_role(user: Optional[Dict[str, Any]], meta: Dict[str, Any]) -> Optional[str]:
    """计算用户在某白板上的有效角色; None=无权访问。"""
    if user is None:
        return None
    if user.get("role") == "admin":
        return "owner"
    if meta.get("owner") == user["username"]:
        return "owner"
    public_role = meta.get("public_role")
    if public_role in VALID_ROLES:
        return public_role
    acl = meta.get("acl") or {}
    role = acl.get(user["username"])
    if role in VALID_ROLES:
        return role
    return None


def role_at_least(role: Optional[str], required: str) -> bool:
    if role is None:
        return False
    return ROLE_ORDER.get(role, 0) >= ROLE_ORDER.get(required, 99)


# ---------------------------------------------------------------- FastAPI 依赖
def _extract_token(request: Request) -> Optional[str]:
    token = request.headers.get("x-auth-token")
    if not token:
        token = request.query_params.get("token")
    return token


def current_user_optional(request: Request) -> Optional[Dict[str, Any]]:
    return user_from_token(_extract_token(request))


def current_user(request: Request) -> Dict[str, Any]:
    user = user_from_token(_extract_token(request))
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user


def require_admin(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
