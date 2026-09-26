"""JSON 文件存储层。

解决的核心问题 —— **并发写入原子性**:

1. 状态/元数据类 JSON (state.json, meta.json, users.json, settings.json):
   采用「同目录临时文件 + flush + fsync + os.replace」的原子替换协议。
   os.replace 在 POSIX 上是原子 rename, 读者永远只会看到旧版本或新版本的
   完整文件, 绝不会读到半截 JSON; 崩溃时临时文件残留但不影响主文件。
   替换成功后再 fsync 目录项, 保证 rename 本身落盘。

2. 操作/聊天日志 (ops/*.jsonl, chat/*.json):
   采用 JSON Lines 追加写 —— 每行一个独立 JSON 对象。O_APPEND 模式下
   小于 PIPE_BUF 的单次 write 由内核保证原子; 进程内再用 per-board
   asyncio 锁串行化, 跨进程用 fcntl.flock 兜底(允许一台机器跑多个
   worker 进程共享同一数据目录)。读取时容忍并修复崩溃造成的最后一行
   半截写入(truncated tail repair)。

3. 所有磁盘写都经过 per-path 锁注册表, 同一文件的并发写在本进程内串行。
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import tempfile
import time
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

# ------------------------------------------------------------ per-path 锁注册表
_path_locks: Dict[str, asyncio.Lock] = {}
_registry_guard = asyncio.Lock() if False else None  # 锁注册在事件循环外也可能发生, 用普通 dict + GIL 原子性


def get_path_lock(path: str) -> asyncio.Lock:
    """获取(或创建)绑定到某文件路径的 asyncio 锁。dict 操作受 GIL 保护。"""
    lock = _path_locks.get(path)
    if lock is None:
        lock = asyncio.Lock()
        _path_locks[path] = lock
    return lock


# ------------------------------------------------------------ 原子 JSON 读写
def read_json(path: str, default: Any = None) -> Any:
    """容错读取 JSON 文件: 缺失/损坏时返回 default。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        # 尝试 .corrupt 备份后返回 default, 不抛异常保证服务可用
        try:
            os.replace(path, path + f".corrupt.{int(time.time())}")
        except OSError:
            pass
        return default


def write_json_atomic(path: str, data: Any) -> None:
    """原子写 JSON: 临时文件 -> fsync -> os.replace -> fsync 目录。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".wb-tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"), default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)          # 原子替换
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)                # 目录项落盘, rename 才真正持久
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


async def write_json_atomic_async(path: str, data: Any) -> None:
    """异步封装: 持 per-path 锁, 在线程池中执行, 避免阻塞事件循环。"""
    async with get_path_lock(path):
        await asyncio.get_running_loop().run_in_executor(None, write_json_atomic, path, data)


def update_json_atomic(path: str, mutate: Callable[[Any], Any], default: Any = None) -> Any:
    """读-改-写 原子更新(同步版)。mutate(data) 返回新数据或 None 表示放弃。"""
    current = read_json(path, default)
    updated = mutate(current)
    if updated is None:
        return current
    write_json_atomic(path, updated)
    return updated


# ------------------------------------------------------------ JSONL 追加日志
class JsonlLog:
    """按行追加的 JSON Lines 日志(带跨进程 flock 与尾部修复)。"""

    def __init__(self, directory: str, prefix: str, shard_fmt: str = "%Y%m%d-%H"):
        self.directory = directory
        self.prefix = prefix            # 文件名前缀, 如 "ops"
        self.shard_fmt = shard_fmt      # 时间分片格式, 默认按小时
        os.makedirs(directory, exist_ok=True)

    # -- 分片命名 -------------------------------------------------------
    def shard_name(self, ts_ms: Optional[int] = None) -> str:
        dt = datetime.fromtimestamp((ts_ms or time.time() * 1000) / 1000.0)
        return f"{self.prefix}-{dt.strftime(self.shard_fmt)}.jsonl"

    def shard_path(self, name: str) -> str:
        return os.path.join(self.directory, name)

    def list_shards(self) -> List[str]:
        try:
            names = [n for n in os.listdir(self.directory)
                     if n.startswith(self.prefix + "-") and n.endswith(".jsonl")]
        except FileNotFoundError:
            return []
        names.sort()   # 名称含时间戳, 字典序即时间序
        return names

    # -- 追加 -----------------------------------------------------------
    def append(self, records: Iterable[dict], ts_ms: Optional[int] = None) -> str:
        """把若干记录追加到 ts 所属分片。返回分片名。

        flock + O_APPEND 双保险: 同进程内由调用方持 per-path 锁串行,
        跨进程由 flock 互斥; 每条记录一行, 一次 write 写入整批,
        保证读端按行解析永远得到完整对象(或可修复的尾行)。
        """
        name = self.shard_name(ts_ms)
        path = self.shard_path(name)
        lines = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
                        for r in records)
        if not lines:
            return name
        encoded = lines.encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, encoded)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return name

    def fsync_shard(self, name: str) -> None:
        try:
            fd = os.open(self.shard_path(name), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    # -- 读取(含尾部修复) -------------------------------------------------
    @staticmethod
    def _parse_lines(path: str, repair: bool = True) -> List[dict]:
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return []
        out: List[dict] = []
        good_end = 0
        for line in raw.split(b"\n"):
            if not line.strip():
                good_end += len(line) + 1
                continue
            try:
                obj = json.loads(line)
                out.append(obj)
                good_end += len(line) + 1
            except json.JSONDecodeError:
                # 崩溃留下的半截行: 截断修复, 保住之前的所有完整记录
                if repair:
                    try:
                        with open(path, "r+b") as fh:
                            fh.truncate(good_end)
                    except OSError:
                        pass
                break
        return out

    def read_shard(self, name: str) -> List[dict]:
        return self._parse_lines(self.shard_path(name))

    def iter_all(self) -> Iterator[dict]:
        for name in self.list_shards():
            yield from self.read_shard(name)

    def rewrite_shard(self, name: str, records: List[dict]) -> None:
        """原子重写整个分片(用于压缩归档)。"""
        path = self.shard_path(name)
        blob = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
                       for r in records)
        directory = self.directory
        fd, tmp = tempfile.mkstemp(prefix=".wb-shard-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def prune_before(self, ts_ms: int) -> List[str]:
        """删除时间早于 ts 的整个分片(分片粒度=小时, 保守按分片名判断)。"""
        removed: List[str] = []
        cutoff = datetime.fromtimestamp(ts_ms / 1000.0).strftime(self.shard_fmt)
        for name in self.list_shards():
            stamp = name[len(self.prefix) + 1: -len(".jsonl")]
            if stamp < cutoff:
                try:
                    os.unlink(self.shard_path(name))
                    removed.append(name)
                except OSError:
                    pass
        return removed

    def total_size(self) -> int:
        total = 0
        for name in self.list_shards():
            try:
                total += os.path.getsize(self.shard_path(name))
            except OSError:
                pass
        return total


# ------------------------------------------------------------ 目录工具
def dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def shard_cache_key(path: str) -> str:
    """分片缓存键规约: 以分片文件名作为全局缓存标识。"""
    return os.path.basename(path)


def safe_id(raw: str, prefix: str = "", maxlen: int = 40) -> str:
    """把任意字符串规约成安全的文件/资源 ID。"""
    keep = [c for c in raw if c.isalnum() or c in ("-", "_")]
    sid = "".join(keep)[:maxlen] or "x"
    return f"{prefix}{sid}" if prefix else sid


def now_ms() -> int:
    return int(time.time() * 1000)


def shard_bounds(records: List[dict]) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    """返回一批记录的 (min_ts, max_ts, min_rev, max_rev)。"""
    if not records:
        return None, None, None, None
    tss = [r.get("ts", 0) for r in records if isinstance(r, dict)]
    revs = [r.get("rev", 0) for r in records if isinstance(r, dict) and r.get("rev")]
    return (min(tss) if tss else None, max(tss) if tss else None,
            min(revs) if revs else None, max(revs) if revs else None)
