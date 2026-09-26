"""全局配置：路径、运行参数、可调系统设置。

系统设置(settings.json)中管理员可在线修改的项通过 DEFAULT_SETTINGS 定义,
其余为进程级常量。所有数据都存放在 DATA_DIR 下的 JSON 文件中:

    data/
      users.json                 用户与会话
      settings.json              系统设置
      templates_custom.json      用户自定义模板
      boards/_index.json         白板索引缓存
      boards/<board_id>/
        meta.json                元数据 + ACL 权限
        state.json               最新状态快照(=最近一次 snapshot)
        ops/YYYYMMDD-HH.jsonl    操作日志, 按小时分片, JSON Lines 追加写
        snapshots/00000123.json  周期性状态快照(以 rev 命名)
        chat/YYYYMMDD.json       聊天消息, 按天分片
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)

DATA_DIR = os.environ.get("WB_DATA_DIR", os.path.join(PROJECT_ROOT, "data"))
FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend")

BOARDS_DIR = os.path.join(DATA_DIR, "boards")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
TEMPLATES_CUSTOM_FILE = os.path.join(DATA_DIR, "templates_custom.json")
BOARD_INDEX_FILE = os.path.join(BOARDS_DIR, "_index.json")

# ---------------------------------------------------------------- 默认系统设置
DEFAULT_SETTINGS: Dict[str, Any] = {
    "site_name": "协同白板 · CoBoard",
    "snapshot_interval_ops": 200,      # 每积累 N 个操作生成一次快照
    "snapshot_interval_secs": 600,     # 或每 N 秒(有操作时)生成一次快照
    "history_retention_days": 30,      # 操作日志保留天数
    "snapshot_keep": 40,               # 每白板保留的快照数量上限
    "max_ops_per_batch": 64,           # 单条 WS 消息最多携带的操作数
    "max_shapes_per_board": 8000,      # 单白板图元数量上限
    "op_flush_interval_ms": 800,       # 操作日志缓冲落盘间隔
    "presence_throttle_ms": 60,        #  presence 广播节流
    "board_idle_unload_secs": 1800,    # 无连接的白板卸载出内存的时间
    "chat_message_max_len": 2000,
    "allow_register": True,            # 是否允许自助注册
    "max_clients_per_board": 32,
    "compact_archived_shards": True,   # 保留期整理时是否物理压缩归档分片
}

# ---------------------------------------------------------------- 进程级常量
SESSION_TTL_SECS = 7 * 24 * 3600        # 会话令牌有效期
WS_HEARTBEAT_SECS = 25                  # 服务端 ping 间隔
WS_TIMEOUT_SECS = 65                    # 超过该时间无任何消息判定断线
RING_BUFFER_OPS = 2000                  # 每白板内存中缓存的最近操作数(快速补发)
UNDO_SERVER_DEPTH = 0                   # 撤销完全由客户端逆操作实现, 服务端不存栈
MOVE_COALESCE_WINDOW_MS = 900           # 回放/压缩时合并连续 move 增量的时间窗
MAX_CATCHUP_OPS = 5000                  # 单次断线补发的最大操作数, 超过则改发全量快照

_settings_cache: Dict[str, Any] = {}
_settings_lock = threading.Lock()


def ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BOARDS_DIR, exist_ok=True)


def get_settings() -> Dict[str, Any]:
    """读取系统设置(带内存缓存, 文件缺失时返回默认值)。"""
    with _settings_lock:
        if _settings_cache:
            return dict(_settings_cache)
    merged = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            for key in DEFAULT_SETTINGS:
                if key in raw:
                    merged[key] = raw[key]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    with _settings_lock:
        _settings_cache.clear()
        _settings_cache.update(merged)
    return dict(merged)


def save_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    """合并保存系统设置(仅接受已定义的键)。"""
    current = get_settings()
    changed = False
    for key, value in patch.items():
        if key in DEFAULT_SETTINGS and value is not None:
            if current.get(key) != value:
                changed = True
            current[key] = value
    if changed:
        from .storage import write_json_atomic
        write_json_atomic(SETTINGS_FILE, current)
    with _settings_lock:
        _settings_cache.clear()
        _settings_cache.update(current)
    return dict(current)


def board_dir(board_id: str) -> str:
    return os.path.join(BOARDS_DIR, board_id)
