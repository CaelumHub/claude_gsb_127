#!/usr/bin/env bash
# 关闭 CoBoard 协同白板（前端 + 后端）
#
# 后端同时托管前端，因此杀掉后端进程即同时关闭前端与后端。
# 优先优雅关闭（SIGTERM），超时后再强制结束（SIGKILL）。
#
# 用法:
#   ./end.sh                  # 关闭默认端口 8300 上的服务
#   WB_PORT=9000 ./end.sh     # 关闭指定端口上的服务
set -euo pipefail

cd "$(dirname "$0")"

PORT="${WB_PORT:-8300}"

# 找出监听该端口的进程（优先按端口；兜底按命令行匹配本项目的 run.py）
PIDS=""
if command -v ss >/dev/null 2>&1; then
  PIDS=$(ss -ltnp "sport = :${PORT}" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u || true)
fi
if [ -z "$PIDS" ]; then
  PIDS=$(pgrep -f "run\.py .*--port ${PORT}([[:space:]]|$)" 2>/dev/null || true)
fi

if [ -z "$PIDS" ]; then
  echo "[end] 未发现运行中的服务（端口 ${PORT}）。"
  exit 0
fi

echo "[end] 正在关闭端口 ${PORT} 上的服务（进程: $(echo "$PIDS" | tr '\n' ' ')）..."
kill $PIDS 2>/dev/null || true

# 最多等 5 秒，端口释放即视为已关闭
for _ in 1 2 3 4 5; do
  if ! ss -ltn "sport = :${PORT}" 2>/dev/null | grep -q ":$PORT"; then
    break
  fi
  sleep 1
done

# 仍未释放则强制结束
if ss -ltn "sport = :${PORT}" 2>/dev/null | grep -q ":$PORT"; then
  echo "[end] 优雅关闭超时，强制结束残留进程..."
  for p in $PIDS; do kill -9 "$p" 2>/dev/null || true; done
  sleep 1
fi

if ss -ltn "sport = :${PORT}" 2>/dev/null | grep -q ":$PORT"; then
  echo "[end] ⚠ 端口 ${PORT} 仍被占用，可能不是本项目的服务，请手动检查。"
  exit 1
fi

echo "[end] 服务已关闭，端口 ${PORT} 已释放。"
