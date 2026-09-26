"""服务端导出: SVG(自绘) / JSON 状态 / 操作日志 NDJSON。

PNG 导出在前端用 canvas.toBlob 完成(所见即所得), 服务端负责不依赖
浏览器的格式。SVG 渲染器支持全部图形种类, 连线自动计算图形边界锚点,
文本按近似字宽折行, 输出可缩放矢量图。
"""
from __future__ import annotations

import asyncio
import html
import json
import math
import time as _time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from . import auth
from .boards import board_ctx, manager
from .crdt import BoardDoc
from .history import history_service

router = APIRouter(prefix="/api/boards", tags=["export"])


# ---------------------------------------------------------------- 几何工具
def _bbox(shape: Dict[str, Any]) -> Tuple[float, float, float, float]:
    x = float(shape.get("x") or 0)
    y = float(shape.get("y") or 0)
    if shape.get("kind") == "path" and shape.get("points"):
        pts = shape["points"]
        xs = [p[0] for p in pts] + [x]
        ys = [p[1] for p in pts] + [y]
        return min(xs), min(ys), max(xs), max(ys)
    return x, y, x + float(shape.get("w") or 0), y + float(shape.get("h") or 0)


def _center(shape: Dict[str, Any]) -> Tuple[float, float]:
    x0, y0, x1, y1 = _bbox(shape)
    return (x0 + x1) / 2.0, (y0 + y1) / 2.0


def _anchor(shape: Dict[str, Any], toward: Tuple[float, float]) -> Tuple[float, float]:
    """从图形中心朝 toward 方向, 求与包围盒边界的交点(连线锚点)。"""
    cx, cy = _center(shape)
    x0, y0, x1, y1 = _bbox(shape)
    dx, dy = toward[0] - cx, toward[1] - cy
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return cx, cy
    hw, hh = (x1 - x0) / 2.0 + 4, (y1 - y0) / 2.0 + 4
    if hw <= 0 or hh <= 0:
        return cx, cy
    scale = min(hw / max(abs(dx), 1e-6), hh / max(abs(dy), 1e-6))
    return cx + dx * scale, cy + dy * scale


def _char_w(ch: str, font_size: float) -> float:
    return font_size * (1.02 if ord(ch) > 0x2e80 else 0.56)


def _wrap_text(text: str, font_size: float, max_w: float, max_lines: int = 30) -> List[str]:
    lines: List[str] = []
    for para in (text or "").split("\n"):
        if not para:
            lines.append("")
            continue
        current = ""
        width = 0.0
        for ch in para:
            cw = _char_w(ch, font_size)
            if width + cw > max_w and current:
                lines.append(current)
                current, width = ch, cw
            else:
                current += ch
                width += cw
        if current:
            lines.append(current)
        if len(lines) > max_lines:
            break
    return lines[:max_lines]


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _style_attrs(shape: Dict[str, Any]) -> str:
    fill = shape.get("fill") or "transparent"
    stroke = shape.get("stroke") or "none"
    sw = float(shape.get("strokeWidth") or 0)
    opacity = float(shape.get("opacity") if shape.get("opacity") is not None else 1)
    dash = shape.get("dash") or []
    attrs = [f'fill="{_esc(fill)}"', f'stroke="{_esc(stroke)}"',
             f'stroke-width="{sw}"', f'opacity="{opacity}"']
    if dash:
        attrs.append('stroke-dasharray="' + " ".join(str(d) for d in dash) + '"')
    return " ".join(attrs)


def _text_svg(shape: Dict[str, Any], cx: float, cy: float, max_w: float,
              anchor_mid: bool = True) -> str:
    text = shape.get("text") or ""
    if not text.strip():
        return ""
    fs = float(shape.get("fontSize") or 14)
    color = shape.get("textColor") or "#1f2937"
    weight = shape.get("fontWeight") or "normal"
    lines = _wrap_text(text, fs, max_w)
    line_h = fs * 1.35
    total_h = line_h * len(lines)
    y = cy - total_h / 2 + fs
    anchor = 'middle' if anchor_mid else 'start'
    tx = cx if anchor_mid else cx - max_w / 2
    out = [f'<text x="{tx:.1f}" y="{y:.1f}" font-size="{fs}" fill="{_esc(color)}" '
           f'font-weight="{_esc(weight)}" text-anchor="{anchor}" '
           f'font-family="PingFang SC, Microsoft YaHei, Noto Sans CJK SC, sans-serif">']
    for line in lines:
        out.append(f'<tspan x="{tx:.1f}" dy="{0 if line is lines[0] else line_h:.1f}">{_esc(line)}</tspan>')
    out.append("</text>")
    return "".join(out)


# ---------------------------------------------------------------- SVG 渲染
def render_svg(shapes: List[Dict[str, Any]], background: str = "#ffffff",
               grid: bool = False, padding: float = 48) -> str:
    alive = [s for s in shapes
             if not s.get("deleted") and s.get("kind") != "ghost"]
    if not alive:
        return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 600">'
                f'<rect width="800" height="600" fill="{background}"/>'
                '<text x="400" y="300" text-anchor="middle" fill="#9aa4b5" '
                'font-size="20">空白板</text></svg>')
    x0 = min(_bbox(s)[0] for s in alive) - padding
    y0 = min(_bbox(s)[1] for s in alive) - padding
    x1 = max(_bbox(s)[2] for s in alive) + padding
    y1 = max(_bbox(s)[3] for s in alive) + padding
    width, height = max(1, x1 - x0), max(1, y1 - y0)
    by_id = {s["id"]: s for s in alive}

    parts: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0:.1f} {y0:.1f} {width:.1f} {height:.1f}" '
        f'width="{width:.0f}" height="{height:.0f}">',
        f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{width:.1f}" height="{height:.1f}" fill="{_esc(background)}"/>',
    ]
    if grid:
        parts.append(
            '<defs><pattern id="wbgrid" width="24" height="24" patternUnits="userSpaceOnUse">'
            '<circle cx="1" cy="1" r="1" fill="#00000014"/></pattern></defs>'
            f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{width:.1f}" height="{height:.1f}" fill="url(#wbgrid)"/>')
    parts.append(
        '<defs><marker id="wbarrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="context-stroke"/></marker></defs>')

    ordered = sorted(alive, key=lambda s: (float(s.get("z") or 0),
                                           (s.get("meta") or {}).get("createdAt") or 0,
                                           s.get("id") or ""))
    # 先画连线类(垫底), 再画节点
    edges = [s for s in ordered if s.get("kind") in ("edge",)]
    nodes = [s for s in ordered if s.get("kind") not in ("edge",)]

    for shape in edges + nodes:
        parts.append(_shape_svg(shape, by_id))
    parts.append("</svg>")
    return "".join(p for p in parts if p)


def _shape_svg(shape: Dict[str, Any], by_id: Dict[str, Dict[str, Any]]) -> str:
    kind = shape.get("kind")
    x, y = float(shape.get("x") or 0), float(shape.get("y") or 0)
    w, h = float(shape.get("w") or 0), float(shape.get("h") or 0)
    style = _style_attrs(shape)

    if kind == "ghost":
        return ""
    if kind == "rect" or kind == "note":
        r = float(shape.get("cornerRadius") or (2 if kind == "rect" else 6))
        shadow = ('<filter id="wbsh"><feDropShadow dx="0" dy="2" stdDeviation="3" '
                  'flood-opacity="0.12"/></filter>') if kind == "note" else ""
        box = (f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{r:.1f}" '
               f'{style} filter="url(#wbsh)"/>' if shadow else
               f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{r:.1f}" {style}/>')
        defs = shadow and f"<defs>{shadow}</defs>" or ""
        return defs + box + _text_svg(shape, x + w / 2, y + h / 2, max(20, w - 16))
    if kind == "mindnode":
        r = min(h / 2, 18)
        box = f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{r:.1f}" {style}/>'
        return box + _text_svg(shape, x + w / 2, y + h / 2, max(20, w - 14))
    if kind == "ellipse":
        return (f'<ellipse cx="{x + w / 2:.1f}" cy="{y + h / 2:.1f}" rx="{max(w / 2, 1):.1f}" '
                f'ry="{max(h / 2, 1):.1f}" {style}/>'
                + _text_svg(shape, x + w / 2, y + h / 2, max(20, w - 20)))
    if kind == "diamond":
        pts = f"{x + w / 2:.1f},{y:.1f} {x + w:.1f},{y + h / 2:.1f} {x + w / 2:.1f},{y + h:.1f} {x:.1f},{y + h / 2:.1f}"
        return f'<polygon points="{pts}" {style}/>' + _text_svg(shape, x + w / 2, y + h / 2, max(20, w / 2))
    if kind == "text":
        return _text_svg(shape, x + w / 2, y + h / 2, max(40, w), anchor_mid=True)
    if kind == "path":
        pts = shape.get("points") or []
        if len(pts) < 2:
            return ""
        d = [f"M {pts[0][0] + x:.1f} {pts[0][1] + y:.1f}"]
        for i in range(1, len(pts)):
            mx = (pts[i - 1][0] + pts[i][0]) / 2 + x
            my = (pts[i - 1][1] + pts[i][1]) / 2 + y
            d.append(f"Q {pts[i - 1][0] + x:.1f} {pts[i - 1][1] + y:.1f} {mx:.1f} {my:.1f}")
        last = pts[-1]
        d.append(f"L {last[0] + x:.1f} {last[1] + y:.1f}")
        sw = float(shape.get("strokeWidth") or 2)
        stroke = shape.get("stroke") or "#374151"
        op = float(shape.get("opacity") if shape.get("opacity") is not None else 1)
        return (f'<path d="{" ".join(d)}" fill="none" stroke="{_esc(stroke)}" '
                f'stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round" opacity="{op}"/>')
    if kind in ("line", "arrow"):
        pts = shape.get("points") or [[0, 0], [w, h]]
        if len(pts) < 2:
            return ""
        (ax, ay), (bx, by_) = (pts[0][0] + x, pts[0][1] + y), (pts[-1][0] + x, pts[-1][1] + y)
        marker = ' marker-end="url(#wbarrow)"' if kind == "arrow" else ""
        stroke = shape.get("stroke") or "#374151"
        sw = float(shape.get("strokeWidth") or 2)
        return (f'<line x1="{ax:.1f}" y1="{ay:.1f}" x2="{bx:.1f}" y2="{by_:.1f}" '
                f'stroke="{_esc(stroke)}" stroke-width="{sw}" stroke-linecap="round"{marker}/>')
    if kind == "edge":
        src = by_id.get(shape.get("from") or "")
        dst = by_id.get(shape.get("to") or "")
        if src is None or dst is None:
            return ""
        sc, dc = _center(src), _center(dst)
        p1 = _anchor(src, dc)
        p2 = _anchor(dst, sc)
        stroke = shape.get("stroke") or "#6b7280"
        sw = float(shape.get("strokeWidth") or 2)
        # 轻微弧线, 双向连线不重叠
        mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        length = math.hypot(dx, dy) or 1
        curve = min(length * 0.12, 40)
        cx_, cy_ = mx - dy / length * curve, my + dx / length * curve
        out = [f'<path d="M {p1[0]:.1f} {p1[1]:.1f} Q {cx_:.1f} {cy_:.1f} {p2[0]:.1f} {p2[1]:.1f}" '
               f'fill="none" stroke="{_esc(stroke)}" stroke-width="{sw}" marker-end="url(#wbarrow)"/>']
        label = shape.get("text") or ""
        if label.strip():
            out.append(f'<text x="{cx_:.1f}" y="{cy_ - 6:.1f}" font-size="{float(shape.get("fontSize") or 12)}" '
                       f'fill="#4b5563" text-anchor="middle">{_esc(label)}</text>')
        return "".join(out)
    return ""


# ---------------------------------------------------------------- 思维导图树边(导出 mindmap 模式时补画层级曲线)
def render_mindmap_links(shapes: List[Dict[str, Any]]) -> str:
    by_id = {s["id"]: s for s in shapes if not s.get("deleted")}
    out: List[str] = []
    for shape in shapes:
        parent_id = shape.get("from")
        if not parent_id or shape.get("deleted") or shape.get("kind") != "mindnode":
            continue
        parent = by_id.get(parent_id)
        if parent is None or parent.get("deleted"):
            continue
        px, py = float(parent.get("x") or 0) + float(parent.get("w") or 0), \
                 float(parent.get("y") or 0) + float(parent.get("h") or 0) / 2
        cx, cy = float(shape.get("x") or 0), float(shape.get("y") or 0) + float(shape.get("h") or 0) / 2
        mid = (px + cx) / 2
        out.append(f'<path d="M {px:.1f} {py:.1f} C {mid:.1f} {py:.1f} {mid:.1f} {cy:.1f} {cx:.1f} {cy:.1f}" '
                   'fill="none" stroke="#9aa4b5" stroke-width="2"/>')
    return "".join(out)


# ---------------------------------------------------------------- 路由
def _download(filename: str, content: str, media_type: str) -> Response:
    quoted = filename.encode("utf-8", "ignore").decode("latin-1", "ignore")
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )


@router.get("/{board_id}/export")
async def export_board(board_id: str,
                       format: str = Query(default="json"),
                       rev: Optional[int] = Query(default=None),
                       background: str = Query(default="#ffffff"),
                       grid: bool = Query(default=False),
                       user: Dict[str, Any] = Depends(auth.current_user)):
    meta, _role = await board_ctx(board_id, user, "viewer")
    hist = history_service.for_board(board_id)
    name = (meta.get("name") or board_id).replace("/", "_")

    if rev is not None:
        folded = await asyncio.get_running_loop().run_in_executor(
            None, hist.fold_window, max(0, rev - 1))
        shapes = folded["shapes"]
    else:
        doc = await manager.get_doc(board_id)
        shapes = doc.visible_shapes()

    if format == "svg":
        doc_shapes = shapes
        body = render_svg(doc_shapes, background=background, grid=grid)
        if meta.get("mode") == "mindmap":
            body = body.replace("</svg>", render_mindmap_links(doc_shapes) + "</svg>")
        return _download(f"{name}.svg", body, "image/svg+xml; charset=utf-8")

    if format == "json":
        doc = manager.docs.get(board_id)
        payload = {
            "format": "coboard-export",
            "version": 1,
            "exported_at": int(_time.time() * 1000),
            "exported_by": user.get("username"),
            "board": {k: meta.get(k) for k in
                      ("id", "name", "mode", "owner", "tags", "created_at", "updated_at")},
            "rev": rev if rev is not None else (doc.head_rev if doc else None),
            "shapes": shapes,
        }
        return _download(f"{name}.json",
                         json.dumps(payload, ensure_ascii=False, indent=2),
                         "application/json; charset=utf-8")

    if format == "ops":
        shard_metas = hist.shards_index()
        total = sum(m.get("count") or 0 for m in shard_metas
                    if (m.get("last_rev") or 0) <= (rev if rev is not None else (1 << 60)))
        lines = [json.dumps(op, ensure_ascii=False)
                 for op in hist.iter_ops(from_rev=0, to_rev=rev)]
        if total:
            lines = lines[:total]
        return _download(f"{name}-ops.ndjson", "\n".join(lines) + "\n",
                         "application/x-ndjson; charset=utf-8")

    raise HTTPException(status_code=400, detail="format 仅支持 json / svg / ops")
