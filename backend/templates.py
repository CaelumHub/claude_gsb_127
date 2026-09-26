"""模板库: 内置模板(程序化生成图形) + 用户自定义模板(从白板保存)。

模板 = 一组带布局坐标的图形定义; 实例化时通过正常的 add_shape 操作
接入 CRDT 管线(见 boards.create_board), 因此模板白板与普通白板在
协同/撤销/回放上没有任何特殊路径。
"""
from __future__ import annotations

import secrets
import time
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException

from . import auth, config
from .boards import board_ctx, manager
from .models import TemplateCreateReq
from .storage import now_ms, read_json, write_json_atomic

router = APIRouter(prefix="/api/templates", tags=["templates"])

NOTE_COLORS = ["#fff9b1", "#ffd6e7", "#d3f0ff", "#d9f7be", "#ffe7ba", "#efdbff"]


# ---------------------------------------------------------------- 图形小工具
def _id(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(5)}"


def _note(x: float, y: float, text: str, fill: str = NOTE_COLORS[0],
          w: float = 180, h: float = 110, pid: Optional[str] = None,
          font: int = 14) -> Dict[str, Any]:
    return {"id": pid or _id("t_n"), "kind": "note", "x": x, "y": y, "w": w, "h": h,
            "text": text, "fill": fill, "fontSize": font, "stroke": "#00000018",
            "strokeWidth": 1, "meta": {"author": "template", "createdAt": now_ms()}}


def _rect(x: float, y: float, w: float, h: float, text: str = "",
          fill: str = "#ffffff", stroke: str = "#4b5563",
          pid: Optional[str] = None, font: int = 14) -> Dict[str, Any]:
    return {"id": pid or _id("t_r"), "kind": "rect", "x": x, "y": y, "w": w, "h": h,
            "text": text, "fill": fill, "stroke": stroke, "strokeWidth": 2,
            "fontSize": font, "cornerRadius": 8,
            "meta": {"author": "template", "createdAt": now_ms()}}


def _ellipse(x: float, y: float, w: float, h: float, text: str = "",
             fill: str = "#e8f1ff", stroke: str = "#3b6fd4",
             pid: Optional[str] = None) -> Dict[str, Any]:
    return {"id": pid or _id("t_e"), "kind": "ellipse", "x": x, "y": y, "w": w, "h": h,
            "text": text, "fill": fill, "stroke": stroke, "strokeWidth": 2, "fontSize": 14,
            "meta": {"author": "template", "createdAt": now_ms()}}


def _diamond(x: float, y: float, w: float, h: float, text: str = "",
             fill: str = "#fff4e0", stroke: str = "#d48806",
             pid: Optional[str] = None) -> Dict[str, Any]:
    return {"id": pid or _id("t_d"), "kind": "diamond", "x": x, "y": y, "w": w, "h": h,
            "text": text, "fill": fill, "stroke": stroke, "strokeWidth": 2, "fontSize": 13,
            "meta": {"author": "template", "createdAt": now_ms()}}


def _edge(src: str, dst: str, label: str = "", color: str = "#6b7280") -> Dict[str, Any]:
    return {"id": _id("t_c"), "kind": "edge", "x": 0, "y": 0, "from": src, "to": dst,
            "text": label, "stroke": color, "strokeWidth": 2, "fontSize": 12,
            "meta": {"author": "template", "createdAt": now_ms()}}


def _text(x: float, y: float, content: str, size: int = 22,
          color: str = "#1f2937", w: float = 400) -> Dict[str, Any]:
    return {"id": _id("t_t"), "kind": "text", "x": x, "y": y, "w": w, "h": size * 1.6,
            "text": content, "fontSize": size, "textColor": color, "fill": "transparent",
            "stroke": "transparent", "strokeWidth": 0,
            "meta": {"author": "template", "createdAt": now_ms()}}


def _mindnode(x: float, y: float, text: str, parent: Optional[str],
              level: int = 1, fill: Optional[str] = None) -> Dict[str, Any]:
    widths = {0: 190, 1: 150, 2: 130}
    heights = {0: 56, 1: 44, 2: 36}
    colors = {0: "#3b6fd4", 1: "#61c0a8", 2: "#f6bd16"}
    return {"id": _id("t_m"), "kind": "mindnode", "x": x, "y": y,
            "w": widths.get(level, 130), "h": heights.get(level, 36),
            "text": text, "parent": parent or None, "collapsed": False,
            "fill": fill or colors.get(level, "#8a93a5"),
            "textColor": "#ffffff" if level <= 1 else "#1f2937",
            "stroke": "#00000022", "strokeWidth": 1, "fontSize": [18, 15, 13][min(level, 2)],
            "meta": {"author": "template", "createdAt": now_ms()}}


# ---------------------------------------------------------------- 内置模板
def tpl_blank_board() -> List[Dict[str, Any]]:
    return []


def tpl_blank_mindmap() -> List[Dict[str, Any]]:
    return [_mindnode(0, 0, "中心主题", None, level=0)]


def tpl_project_plan() -> List[Dict[str, Any]]:
    """项目规划思维导图(三层)。"""
    root = _mindnode(0, 0, "新产品发布", None, level=0)
    out = [root]
    branches = [
        ("需求调研", ["用户访谈", "竞品分析", "问卷投放"]),
        ("产品设计", ["原型设计", "视觉规范", "交互评审"]),
        ("研发实现", ["前端开发", "后端开发", "联调测试"]),
        ("市场推广", ["预热文案", "发布会", "渠道投放"]),
        ("上线运维", ["灰度发布", "监控告警", "复盘总结"]),
    ]
    y = -320
    for bname, children in branches:
        branch = _mindnode(340, y, bname, root["id"], level=1)
        out.append(branch)
        cy = y - ((len(children) - 1) * 70) / 2
        for cname in children:
            out.append(_mindnode(620, cy, cname, branch["id"], level=2))
            cy += 70
        y += 165
    return out


def tpl_flowchart() -> List[Dict[str, Any]]:
    """审批流程图。"""
    start = _ellipse(340, 20, 140, 56, "开始", pid="t_f_start")
    submit = _rect(320, 130, 180, 64, "提交申请", pid="t_f_submit")
    review = _rect(320, 250, 180, 64, "主管审核", pid="t_f_review")
    decision = _diamond(310, 370, 200, 110, "金额 > 5000?", pid="t_f_dec")
    director = _rect(600, 395, 180, 64, "总监审批", pid="t_f_dir")
    finance = _rect(320, 540, 180, 64, "财务打款", pid="t_f_fin")
    reject = _rect(60, 395, 160, 64, "驳回并通知", fill="#ffe3e3",
                   stroke="#d43d3d", pid="t_f_rej")
    done = _ellipse(340, 660, 140, 56, "结束", pid="t_f_end")
    edges = [
        _edge(start["id"], submit["id"]),
        _edge(submit["id"], review["id"]),
        _edge(review["id"], decision["id"]),
        _edge(decision["id"], director["id"], "是"),
        _edge(decision["id"], finance["id"], "否"),
        _edge(decision["id"], reject["id"], "不合规"),
        _edge(director["id"], finance["id"], "通过"),
        _edge(director["id"], reject["id"], "拒绝"),
        _edge(finance["id"], done["id"]),
        _edge(reject["id"], done["id"]),
    ]
    return [start, submit, review, decision, director, finance, reject, done] + edges


def tpl_swot() -> List[Dict[str, Any]]:
    """SWOT 四象限。"""
    quads = [
        ("S 优势 Strengths", "#d9f7be", 60, 140, "• 核心团队经验丰富\n• 产品口碑良好"),
        ("W 劣势 Weaknesses", "#ffe7ba", 520, 140, "• 渠道覆盖不足\n• 品牌知名度低"),
        ("O 机会 Opportunities", "#d3f0ff", 60, 420, "• 新兴市场增长快\n• 政策扶持"),
        ("T 威胁 Threats", "#ffd6e7", 520, 420, "• 竞品价格战\n• 供应链波动"),
    ]
    out: List[Dict[str, Any]] = [_text(60, 40, "SWOT 分析", 30, "#1f2937", 500)]
    for title, fill, x, y, body in quads:
        out.append(_rect(x, y, 420, 60, title, fill=fill, stroke="#00000020", font=17))
        out.append(_note(x, y + 70, body, fill="#ffffff", w=420, h=200, font=15))
    return out


def tpl_kanban() -> List[Dict[str, Any]]:
    """看板: 三列 + 示例卡片。"""
    out: List[Dict[str, Any]] = []
    cols = [("待办", "#e8f1ff", 40), ("进行中", "#fff4e0", 380), ("已完成", "#e6fffb", 720)]
    cards = {
        "待办": ["撰写需求文档", "设计数据库表结构", "约用户访谈"],
        "进行中": ["登录模块开发", "首页视觉稿"],
        "已完成": ["项目立项", "技术选型评审"],
    }
    for name, fill, x in cols:
        out.append(_rect(x, 60, 300, 70, name, fill=fill, stroke="#00000018", font=18))
        y = 160
        for card in cards[name]:
            out.append(_note(x + 10, y, card, fill="#ffffff", w=280, h=96, font=14))
            y += 116
    return out


def tpl_weekly() -> List[Dict[str, Any]]:
    """周计划: 7 列便签。"""
    days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    out: List[Dict[str, Any]] = [_text(40, 20, "本周计划", 28)]
    x = 40
    for i, day in enumerate(days):
        out.append(_rect(x, 90, 170, 46, day, fill="#e8f1ff", stroke="#00000018", font=15))
        out.append(_note(x, 150, "", fill=NOTE_COLORS[i % len(NOTE_COLORS)], w=170, h=320, font=13))
        x += 190
    return out


def tpl_user_journey() -> List[Dict[str, Any]]:
    """用户旅程地图。"""
    stages = ["认知", "考虑", "购买", "使用", "推荐"]
    out: List[Dict[str, Any]] = [_text(40, 20, "用户旅程地图", 28)]
    x = 40
    prev: Optional[str] = None
    for i, stage in enumerate(stages):
        node = _ellipse(x, 100, 150, 70, stage, pid=f"t_j_s{i}")
        out.append(node)
        if prev:
            out.append(_edge(prev, node["id"]))
        prev = node["id"]
        out.append(_note(x - 10, 220, "触点:\n\n情绪: 🙂\n机会点:", fill=NOTE_COLORS[i % len(NOTE_COLORS)], w=180, h=200, font=13))
        x += 230
    return out


def tpl_brainstorm() -> List[Dict[str, Any]]:
    """头脑风暴: 中心话题 + 放射便签。"""
    center = _ellipse(380, 300, 220, 110, "如何提升留存?", fill="#3b6fd4", stroke="#2b52a3")
    center["textColor"] = "#ffffff"
    center["fontSize"] = 18
    out: List[Dict[str, Any]] = [center]
    ideas = [
        (80, 80, "新手引导重做"), (700, 80, "激励体系"), (40, 380, "性能优化"),
        (760, 380, "社区氛围"), (380, 20, "个性化推荐"), (380, 540, "召回推送"),
    ]
    for i, (x, y, text) in enumerate(ideas):
        note = _note(x, y, text, fill=NOTE_COLORS[i % len(NOTE_COLORS)], w=170, h=90, font=14)
        out.append(note)
        out.append(_edge(center["id"], note["id"], color="#9aa4b5"))
    return out


def tpl_meeting() -> List[Dict[str, Any]]:
    """会议纪要。"""
    out = [_text(60, 40, "会议纪要", 32)]
    out.append(_note(60, 130, "会议主题:\n时间:\n参与人:\n记录人:", fill="#ffffff", w=420, h=150, font=14))
    out.append(_note(520, 130, "决议事项\n1.\n2.\n3.", fill="#d9f7be", w=420, h=150, font=14))
    out.append(_note(60, 320, "讨论要点\n•\n•\n•", fill="#d3f0ff", w=420, h=190, font=14))
    out.append(_note(520, 320, "行动项(负责人/截止)\n1.\n2.\n3.", fill="#ffe7ba", w=420, h=190, font=14))
    return out


def tpl_org_chart() -> List[Dict[str, Any]]:
    """组织架构(思维导图结构)。"""
    root = _mindnode(0, 0, "CEO", None, level=0)
    out = [root]
    deps = [("技术部", ["前端组", "后端组", "测试组"]),
            ("产品部", ["产品一组", "产品二组"]),
            ("市场部", ["品牌", "渠道"])]
    y = -200
    for dname, teams in deps:
        dep = _mindnode(320, y, dname, root["id"], level=1)
        out.append(dep)
        ty = y - ((len(teams) - 1) * 70) / 2
        for tname in teams:
            out.append(_mindnode(600, ty, tname, dep["id"], level=2))
            ty += 70
        y += 200
    return out


BUILTIN_TEMPLATES: List[Dict[str, Any]] = [
    {"id": "blank-board", "name": "空白白板", "category": "基础", "mode": "board",
     "description": "从零开始的自由画布", "builder": tpl_blank_board},
    {"id": "blank-mindmap", "name": "空白思维导图", "category": "基础", "mode": "mindmap",
     "description": "只有一个中心主题, 按 Tab 生长", "builder": tpl_blank_mindmap},
    {"id": "project-plan", "name": "项目规划", "category": "思维导图", "mode": "mindmap",
     "description": "五阶段项目拆解, 三层结构", "builder": tpl_project_plan},
    {"id": "org-chart", "name": "组织架构", "category": "思维导图", "mode": "mindmap",
     "description": "公司-部门-团队三级架构", "builder": tpl_org_chart},
    {"id": "flowchart", "name": "审批流程图", "category": "流程图", "mode": "board",
     "description": "含判断分支与驳回路径", "builder": tpl_flowchart},
    {"id": "swot", "name": "SWOT 分析", "category": "分析", "mode": "board",
     "description": "四象限优劣势机会威胁", "builder": tpl_swot},
    {"id": "kanban", "name": "看板", "category": "规划", "mode": "board",
     "description": "待办/进行中/已完成三列", "builder": tpl_kanban},
    {"id": "weekly", "name": "周计划", "category": "规划", "mode": "board",
     "description": "七天便签墙", "builder": tpl_weekly},
    {"id": "user-journey", "name": "用户旅程", "category": "分析", "mode": "board",
     "description": "五阶段旅程 + 触点便签", "builder": tpl_user_journey},
    {"id": "brainstorm", "name": "头脑风暴", "category": "创意", "mode": "board",
     "description": "中心话题放射式便签", "builder": tpl_brainstorm},
    {"id": "meeting", "name": "会议纪要", "category": "效率", "mode": "board",
     "description": "议题/要点/决议/行动项", "builder": tpl_meeting},
]

BUILTIN_INDEX: Dict[str, Dict[str, Any]] = {t["id"]: t for t in BUILTIN_TEMPLATES}


def build_template_shapes(template_id: str, mode: str = "board") -> List[Dict[str, Any]]:
    """实例化模板: 返回带全新 id 的图形列表(parent/from/to 同步重映射)。"""
    tpl = BUILTIN_INDEX.get(template_id)
    if tpl is None:
        custom = _load_custom().get(template_id)
        if custom is None:
            return []
        shapes = custom.get("shapes") or []
        return _remap_ids(shapes)
    shapes = tpl["builder"]()
    return _remap_ids(shapes)


def _remap_ids(shapes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    id_map: Dict[str, str] = {}
    out: List[Dict[str, Any]] = []
    for shape in shapes:
        new_shape = dict(shape)
        old_id = str(shape.get("id") or "")
        new_id = _id("s")
        if old_id:
            id_map[old_id] = new_id
        new_shape["id"] = new_id
        new_shape.pop("fc", None)
        out.append(new_shape)
    for shape in out:
        for field in ("from", "to"):
            ref = shape.get(field)
            if isinstance(ref, str) and ref in id_map:
                shape[field] = id_map[ref]
    return out


# ---------------------------------------------------------------- 自定义模板
def _load_custom() -> Dict[str, Dict[str, Any]]:
    data = read_json(config.TEMPLATES_CUSTOM_FILE, default={}) or {}
    return data.get("templates") if isinstance(data.get("templates"), dict) else {}


def _save_custom(templates: Dict[str, Dict[str, Any]]) -> None:
    write_json_atomic(config.TEMPLATES_CUSTOM_FILE,
                      {"templates": templates, "updated_at": now_ms()})


def _public_tpl(tpl: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in tpl.items() if k not in ("builder", "shapes")}


# ---------------------------------------------------------------- REST 路由
@router.get("")
async def list_templates(user: Dict[str, Any] = Depends(auth.current_user)):
    builtins = [_public_tpl(t) | {"builtin": True, "shape_hint": t["id"]}
                for t in BUILTIN_TEMPLATES]
    customs = [_public_tpl(t) | {"builtin": False} for t in _load_custom().values()]
    categories = sorted({t.get("category", "其他") for t in builtins + customs}, reverse=True)
    return {"templates": builtins + customs, "categories": categories}


@router.get("/{template_id}/shapes")
async def template_shapes(template_id: str,
                          user: Dict[str, Any] = Depends(auth.current_user)):
    shapes = build_template_shapes(template_id)
    if not shapes and template_id not in BUILTIN_INDEX and template_id not in _load_custom():
        raise HTTPException(status_code=404, detail="模板不存在")
    return {"template_id": template_id, "shapes": shapes}


@router.post("")
async def create_template(req: TemplateCreateReq,
                          user: Dict[str, Any] = Depends(auth.current_user)):
    templates = _load_custom()
    tpl_id = "tpl" + secrets.token_hex(5)
    shapes: List[Dict[str, Any]] = []
    if req.from_board:
        meta, _role = await board_ctx(req.from_board, user, "viewer")
        doc = await manager.get_doc(meta["id"])
        for shape in doc.visible_shapes():
            if len(shapes) >= 400:
                break
            clean = dict(shape)
            clean.pop("fc", None)
            shapes.append(clean)
        if not shapes:
            raise HTTPException(status_code=400, detail="白板为空, 无法保存为模板")
    elif isinstance(req.definition, dict) and isinstance(req.definition.get("shapes"), list):
        shapes = req.definition["shapes"][:400]
    else:
        raise HTTPException(status_code=400, detail="需要 from_board 或 definition.shapes")
    templates[tpl_id] = {
        "id": tpl_id,
        "name": req.name[:60],
        "category": (req.category or "自定义")[:20],
        "description": (req.description or "")[:200],
        "mode": "board",
        "owner": user["username"],
        "created_at": now_ms(),
        "shapes": shapes,
    }
    _save_custom(templates)
    return {"template": _public_tpl(templates[tpl_id]) | {"builtin": False}}


@router.delete("/{template_id}")
async def delete_template(template_id: str,
                          user: Dict[str, Any] = Depends(auth.current_user)):
    if template_id in BUILTIN_INDEX:
        raise HTTPException(status_code=400, detail="内置模板不可删除")
    templates = _load_custom()
    tpl = templates.get(template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    if tpl.get("owner") != user["username"] and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅模板创建者或管理员可删除")
    templates.pop(template_id)
    _save_custom(templates)
    return {"ok": True}
