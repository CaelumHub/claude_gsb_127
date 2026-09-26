"""协同白板与思维导图工具 —— 后端包。

模块划分:
    config    全局配置与路径
    models    Pydantic 请求/响应模型
    storage   原子 JSON 存储、时间分片 JSONL 日志、快照仓库
    crdt      操作型 CRDT 引擎（增量移动合成 + 字段级 LWW 寄存器）
    history   操作历史、压缩、快速回放窗口
    auth      用户、会话、角色与白板级权限
    boards    白板 CRUD / 状态 / 权限 REST 路由
    chat      协作聊天 REST 路由
    templates 模板库
    export    服务端导出（SVG/JSON/操作日志）
    replay    历史回放 REST 路由
    ws        WebSocket 房间、实时同步、断线补发
    seed      首次启动演示数据播种
    main      FastAPI 应用装配与后台任务
"""
__version__ = "1.0.0"
