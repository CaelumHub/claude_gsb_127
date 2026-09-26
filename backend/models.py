"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- 认证
class RegisterReq(BaseModel):
    username: str = Field(min_length=2, max_length=24)
    password: str = Field(min_length=4, max_length=64)
    display_name: Optional[str] = Field(default=None, max_length=40)


class LoginReq(BaseModel):
    username: str
    password: str


class UserPatchReq(BaseModel):
    role: Optional[str] = None            # admin | user
    display_name: Optional[str] = None
    disabled: Optional[bool] = None
    color: Optional[str] = None
    password: Optional[str] = Field(default=None, min_length=4, max_length=64)


# ---------------------------------------------------------------- 白板
class BoardCreateReq(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    mode: str = Field(default="board")    # board | mindmap
    template_id: Optional[str] = None
    tags: Optional[List[str]] = None


class BoardPatchReq(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    mode: Optional[str] = None
    tags: Optional[List[str]] = None
    thumbnail: Optional[str] = Field(default=None, max_length=60000)   # dataURL


class PermissionsReq(BaseModel):
    acl: Dict[str, str] = {}              # username → editor|commenter|viewer
    public_role: Optional[str] = None     # 邀请链接访客角色(可空=不开放)


class DuplicateReq(BaseModel):
    name: Optional[str] = None


# ---------------------------------------------------------------- 聊天
class ChatPostReq(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    kind: str = Field(default="msg")      # msg | system


# ---------------------------------------------------------------- 模板
class TemplateCreateReq(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    category: str = Field(default="自定义", max_length=20)
    description: Optional[str] = Field(default="", max_length=200)
    from_board: Optional[str] = None      # 从现有白板保存
    definition: Optional[Dict[str, Any]] = None   # 或直接给定义


# ---------------------------------------------------------------- 设置
class SettingsPatchReq(BaseModel):
    settings: Dict[str, Any]


# ---------------------------------------------------------------- 回放/历史
class CompactReq(BaseModel):
    confirm: bool = False


def limit_catchup(gap: int, ring_capacity: int, max_catchup: int) -> bool:
    """判断断线补发是否应直接降级为全量快照。"""
    return gap > ring_capacity and gap > max_catchup
