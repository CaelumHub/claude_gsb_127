#!/usr/bin/env python3
"""协同白板与思维导图工具 —— 启动入口。

用法:
    python run.py                # 默认 127.0.0.1:8300
    python run.py --host 0.0.0.0 --port 9000
    python run.py --reset-demo   # 清空演示数据后重新播种
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main() -> None:
    parser = argparse.ArgumentParser(description="协同白板服务器")
    parser.add_argument("--host", default=os.environ.get("WB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WB_PORT", "8300")))
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    parser.add_argument("--reset-demo", action="store_true", help="重置演示数据")
    args = parser.parse_args()

    if args.reset_demo:
        from backend import seed
        seed.reset_demo_data()
        print("[run] 演示数据已重置")

    import uvicorn
    print(f"[run] 协同白板服务启动: http://{args.host}:{args.port}")
    print("[run] 演示账号: demo / demo123  (管理员)   alice / alice123   bob / bob123")
    uvicorn.run(
        "backend.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        ws_ping_interval=None,   # 心跳由应用层自己管理（presence/ping 消息）
    )


if __name__ == "__main__":
    main()
