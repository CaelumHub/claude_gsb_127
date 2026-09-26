#!/usr/bin/env bash
# 一键启动 CoBoard 协同白板（前端 + 后端）
#
# 前端是纯静态页面，由后端 FastAPI 同源托管，启动这一个进程即可同时跑起前端与后端。
# 服务始终在「前台」运行（无后台子进程），日志直接打印到当前终端，按 Ctrl+C 即可停止。
#
# 用法:
#   ./start.sh                # 默认 http://127.0.0.1:8300
#   ./start.sh --reload       # 开发模式热重载
#   ./start.sh --reset-demo   # 重置演示数据
#   WB_HOST=0.0.0.0 WB_PORT=9000 ./start.sh
set -euo pipefail

cd "$(dirname "$0")"

HOST="${WB_HOST:-127.0.0.1}"
PORT="${WB_PORT:-8300}"

# 0.0.0.0 / 127.0.0.1 都用 localhost 作为展示与探活地址，方便本机浏览器访问
if [ "$HOST" = "0.0.0.0" ] || [ "$HOST" = "127.0.0.1" ]; then
  SHOW_HOST="localhost"
  CHECK_URL="http://127.0.0.1:${PORT}/api/system/health"
else
  SHOW_HOST="$HOST"
  CHECK_URL="http://${HOST}:${PORT}/api/system/health"
fi
FRONTEND_URL="http://${SHOW_HOST}:${PORT}/"

banner() {
  echo "========================================================"
  echo "  CoBoard 协同白板 · 前端 + 后端一键启动"
  echo "========================================================"
  echo "  前端地址  : ${FRONTEND_URL}"
  echo "  演示账号  : demo / demo123   (管理员)"
  echo "            : alice / alice123"
  echo "            : bob   / bob123"
  echo "  停止服务  : 按 Ctrl+C"
  echo "  (前端与后端同源，同一个服务监听端口 ${PORT})"
  echo "========================================================"
  echo ""
}

# 已经在运行：直接给出地址，幂等返回
if curl -s --max-time 2 "${CHECK_URL}" | grep -q '"ok"'; then
  banner
  echo "[start] 服务已在运行，无需重复启动。"
  exit 0
fi

# 依赖缺失时自动创建虚拟环境并安装（已装好则直接跳过）
PY="python3"
if ! "$PY" -c "import fastapi, uvicorn, pydantic, websockets" >/dev/null 2>&1; then
  echo "[start] 依赖未安装，自动创建 .venv 并安装 requirements.txt ..."
  if [ ! -d ".venv" ]; then
    "$PY" -m venv .venv
  fi
  PY="$(pwd)/.venv/bin/python"
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q -r requirements.txt
fi

banner

# 前台运行：exec 让 python 接管当前终端进程，Ctrl+C 信号直达 uvicorn，可正常停止
exec "$PY" run.py --host "$HOST" --port "$PORT" "$@"
