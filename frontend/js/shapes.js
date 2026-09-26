/* ================================================================
   shapes.js —— 图形模型: 工厂 / 包围盒 / 命中测试 / 连线锚点 /
   空间索引(均匀网格) / 文本测量
   ================================================================ */

export const KIND_LABEL = {
  path: '手绘', line: '直线', arrow: '箭头', rect: '矩形', ellipse: '椭圆',
  diamond: '菱形', note: '便签', text: '文本', edge: '连线', mindnode: '思维节点',
};

export const NOTE_PALETTE = ['#fff9b1', '#ffd6e7', '#d3f0ff', '#d9f7be', '#ffe7ba', '#efdbff'];
export const STROKE_PALETTE = ['#374151', '#e8684a', '#3b6fd4', '#2e9e6b', '#d48806', '#7e5fd0', '#d4526a', '#8a93a5'];

let zCounter = 1;
export function resetZCounter(maxZ = 0) { zCounter = Math.max(1, maxZ + 1); }
export function nextZ() { return zCounter++; }
export function syncZCounter(shapes) {
  let max = 0;
  for (const s of shapes.values ? shapes.values() : shapes) {
    max = Math.max(max, Number(s.z) || 0);
  }
  zCounter = max + 1;
}

export function uidShape() {
  const rand = (crypto?.randomUUID ? crypto.randomUUID().replace(/-/g, '') : Math.random().toString(36).slice(2));
  return `sh_${rand.slice(0, 14)}`;
}

/* ---------------------------------------------------------------- 工厂 */
export function createShape(kind, props = {}) {
  const base = {
    id: props.id || uidShape(),
    kind,
    x: props.x ?? 0, y: props.y ?? 0,
    w: props.w ?? 0, h: props.h ?? 0,
    z: props.z ?? nextZ(),
    text: props.text ?? '',
    deleted: false,
    meta: { author: props.author || '', createdAt: Date.now() },
  };
  const defaults = {
    rect: { w: 160, h: 100, fill: '#ffffff', stroke: '#4b5563', strokeWidth: 2, cornerRadius: 8, fontSize: 14, textColor: '#1f2937' },
    ellipse: { w: 150, h: 90, fill: '#e8f1ff', stroke: '#3b6fd4', strokeWidth: 2, fontSize: 14, textColor: '#1f2937' },
    diamond: { w: 160, h: 100, fill: '#fff4e0', stroke: '#d48806', strokeWidth: 2, fontSize: 13, textColor: '#1f2937' },
    note: { w: 190, h: 120, fill: NOTE_PALETTE[Math.floor(Math.random() * NOTE_PALETTE.length)], stroke: 'rgba(0,0,0,0.08)', strokeWidth: 1, fontSize: 14, textColor: '#333', cornerRadius: 4 },
    text: { w: 260, h: 40, fill: 'transparent', stroke: 'transparent', strokeWidth: 0, fontSize: 20, textColor: '#e6e9f0' },
    path: { fill: 'transparent', stroke: '#374151', strokeWidth: 3, points: [] },
    line: { fill: 'transparent', stroke: '#374151', strokeWidth: 2, points: [] },
    arrow: { fill: 'transparent', stroke: '#374151', strokeWidth: 2, points: [] },
    edge: { fill: 'transparent', stroke: '#6b7280', strokeWidth: 2, fontSize: 12, textColor: '#6b7280', from: null, to: null },
    mindnode: { w: 150, h: 44, fill: '#61c0a8', stroke: 'rgba(0,0,0,0.12)', strokeWidth: 1, fontSize: 15, textColor: '#fff', cornerRadius: 22, collapsed: false, parent: null },
  };
  return { ...base, ...(defaults[kind] || {}), ...props, id: base.id, kind, deleted: false };
}

/* ---------------------------------------------------------------- 几何 */
export function bboxOf(shape) {
  const x = Number(shape.x) || 0;
  const y = Number(shape.y) || 0;
  if ((shape.kind === 'path' || shape.kind === 'line' || shape.kind === 'arrow') && shape.points?.length) {
    let x0 = Infinity; let y0 = Infinity; let x1 = -Infinity; let y1 = -Infinity;
    for (const [px, py] of shape.points) {
      if (px < x0) x0 = px;
      if (px > x1) x1 = px;
      if (py < y0) y0 = py;
      if (py > y1) y1 = py;
    }
    return { x0: x + x0, y0: y + y0, x1: x + x1, y1: y + y1 };
  }
  const w = Number(shape.w) || 0;
  const h = Number(shape.h) || 0;
  return { x0: x, y0: y, x1: x + w, y1: y + h };
}

export function centerOf(shape) {
  const b = bboxOf(shape);
  return { x: (b.x0 + b.x1) / 2, y: (b.y0 + b.y1) / 2 };
}

export function bboxIntersects(a, b, pad = 0) {
  return a.x0 - pad <= b.x1 && a.x1 + pad >= b.x0 && a.y0 - pad <= b.y1 && a.y1 + pad >= b.y0;
}

export function expandBbox(target, b) {
  target.x0 = Math.min(target.x0, b.x0);
  target.y0 = Math.min(target.y0, b.y0);
  target.x1 = Math.max(target.x1, b.x1);
  target.y1 = Math.max(target.y1, b.y1);
  return target;
}

export function emptyBbox() {
  return { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity };
}

export function shapesBbox(shapes) {
  let b = emptyBbox();
  let any = false;
  for (const s of shapes) { b = expandBbox(b, bboxOf(s)); any = true; }
  return any ? b : { x0: 0, y0: 0, x1: 100, y1: 100 };
}

function distToSegment(px, py, ax, ay, bx, by) {
  const dx = bx - ax; const dy = by - ay;
  const len2 = dx * dx + dy * dy;
  let t = len2 ? ((px - ax) * dx + (py - ay) * dy) / len2 : 0;
  t = Math.max(0, Math.min(1, t));
  const cx = ax + t * dx; const cy = ay + t * dy;
  return Math.hypot(px - cx, py - cy);
}

/** 世界坐标点是否命中图形; tol 为容差(世界单位) */
export function hitTest(shape, wx, wy, tol = 6) {
  if (!shape || shape.deleted || shape.kind === 'ghost') return false;
  const b = bboxOf(shape);
  if (shape.kind === 'path' || shape.kind === 'line' || shape.kind === 'arrow') {
    const pts = shape.points || [];
    const ox = Number(shape.x) || 0; const oy = Number(shape.y) || 0;
    for (let i = 1; i < pts.length; i++) {
      if (distToSegment(wx, wy, pts[i - 1][0] + ox, pts[i - 1][1] + oy, pts[i][0] + ox, pts[i][1] + oy) <= tol + (Number(shape.strokeWidth) || 2) / 2) return true;
    }
    return false;
  }
  if (shape.kind === 'edge') {
    // 连线命中由调用方解析端点后单独处理(见 hitTestEdge)
    return wx >= b.x0 - tol && wx <= b.x1 + tol && wy >= b.y0 - tol && wy <= b.y1 + tol;
  }
  if (shape.kind === 'ellipse') {
    const cx = (b.x0 + b.x1) / 2; const cy = (b.y0 + b.y1) / 2;
    const rx = (b.x1 - b.x0) / 2 + tol; const ry = (b.y1 - b.y0) / 2 + tol;
    if (rx <= 0 || ry <= 0) return false;
    const nx = (wx - cx) / rx; const ny = (wy - cy) / ry;
    return nx * nx + ny * ny <= 1;
  }
  if (shape.kind === 'diamond') {
    const cx = (b.x0 + b.x1) / 2; const cy = (b.y0 + b.y1) / 2;
    const hw = (b.x1 - b.x0) / 2 + tol; const hh = (b.y1 - b.y0) / 2 + tol;
    return Math.abs(wx - cx) / hw + Math.abs(wy - cy) / hh <= 1;
  }
  return wx >= b.x0 - tol && wx <= b.x1 + tol && wy >= b.y0 - tol && wy <= b.y1 + tol;
}

/** 连线锚点: 从图形中心朝目标点方向与包围盒边界的交点 */
export function anchorPoint(shape, towardX, towardY) {
  const b = bboxOf(shape);
  const cx = (b.x0 + b.x1) / 2; const cy = (b.y0 + b.y1) / 2;
  const dx = towardX - cx; const dy = towardY - cy;
  if (Math.abs(dx) < 1e-6 && Math.abs(dy) < 1e-6) return { x: cx, y: cy };
  const hw = (b.x1 - b.x0) / 2 + 4; const hh = (b.y1 - b.y0) / 2 + 4;
  const scale = Math.min(hw / Math.max(Math.abs(dx), 1e-6), hh / Math.max(Math.abs(dy), 1e-6));
  return { x: cx + dx * scale, y: cy + dy * scale };
}

/** 解析 edge 的两个端点锚点 */
export function edgeEndpoints(shape, shapesMap) {
  const src = shapesMap.get(shape.from);
  const dst = shapesMap.get(shape.to);
  if (!src || !dst || src.deleted || dst.deleted) return null;
  const sc = centerOf(src); const dc = centerOf(dst);
  return { p1: anchorPoint(src, dc.x, dc.y), p2: anchorPoint(dst, sc.x, sc.y), src, dst };
}

/* ---------------------------------------------------------------- 文本 */
let _measureCanvas = null;
export function measureText(text, fontSize = 14, fontWeight = 'normal') {
  if (!_measureCanvas) _measureCanvas = document.createElement('canvas');
  const ctx = _measureCanvas.getContext('2d');
  ctx.font = `${fontWeight} ${fontSize}px "PingFang SC","Microsoft YaHei",sans-serif`;
  return ctx.measureText(text).width;
}

/** 按最大宽度折行(中英文混排, 逐字符) */
export function wrapText(text, fontSize, maxW, maxLines = 40) {
  const lines = [];
  for (const para of String(text ?? '').split('\n')) {
    if (!para) { lines.push(''); continue; }
    let current = '';
    let width = 0;
    for (const ch of para) {
      const cw = ch.charCodeAt(0) > 0x2e80 ? fontSize * 1.02 : fontSize * 0.56;
      if (width + cw > maxW && current) { lines.push(current); current = ch; width = cw; }
      else { current += ch; width += cw; }
    }
    if (current) lines.push(current);
    if (lines.length > maxLines) break;
  }
  return lines;
}

/** 估算思维节点尺寸 */
export function mindNodeSize(text, level = 1) {
  const fontSize = [18, 15, 13][Math.min(level, 2)];
  const w = Math.max(level === 0 ? 150 : 100, Math.min(320, measureText(text || '主题', fontSize, '600') + 44));
  const h = [56, 44, 36][Math.min(level, 2)];
  return { w, h, fontSize };
}

/* ---------------------------------------------------------------- 空间索引
   均匀网格哈希: cell = 256 世界单位。增删改 O(覆盖格数),
   视口裁剪与命中测试只遍历候选格, 支撑大画布(数千图形)交互。 */
export class SpatialGrid {
  constructor(cellSize = 256) {
    this.cell = cellSize;
    this.map = new Map();           // "cx,cy" → Set(id)
  }

  _keys(b) {
    const c = this.cell;
    const x0 = Math.floor(b.x0 / c); const x1 = Math.floor(b.x1 / c);
    const y0 = Math.floor(b.y0 / c); const y1 = Math.floor(b.y1 / c);
    const keys = [];
    for (let x = x0; x <= x1; x++) for (let y = y0; y <= y1; y++) keys.push(`${x},${y}`);
    return keys;
  }

  clear() { this.map.clear(); }

  insert(shape) {
    for (const key of this._keys(bboxOf(shape))) {
      let set = this.map.get(key);
      if (!set) { set = new Set(); this.map.set(key, set); }
      set.add(shape.id);
    }
  }

  remove(shape) {
    for (const key of this._keys(bboxOf(shape))) {
      const set = this.map.get(key);
      if (set) { set.delete(shape.id); if (!set.size) this.map.delete(key); }
    }
  }

  /** 图形移动/变形后重建其索引 */
  refresh(shape) { this.removeById(shape.id); this.insert(shape); }

  removeById(id) {
    for (const [key, set] of this.map) {
      if (set.delete(id) && !set.size) this.map.delete(key);
    }
  }

  /** 查询与矩形相交的候选 id */
  query(b) {
    const out = new Set();
    for (const key of this._keys(b)) {
      const set = this.map.get(key);
      if (set) set.forEach((id) => out.add(id));
    }
    return out;
  }

  rebuild(shapes) {
    this.clear();
    for (const s of shapes) if (!s.deleted) this.insert(s);
  }
}

/* ---------------------------------------------------------------- 平滑 */
/** 手绘抽稀: 距离小于 minDist 的点丢弃(减少点数, 渲染更快) */
export function simplifyPoints(points, minDist = 2.2) {
  if (points.length < 3) return points;
  const out = [points[0]];
  for (let i = 1; i < points.length - 1; i++) {
    const last = out[out.length - 1];
    if (Math.hypot(points[i][0] - last[0], points[i][1] - last[1]) >= minDist) out.push(points[i]);
  }
  out.push(points[points.length - 1]);
  return out;
}

/** 构造平滑 Path2D(二次贝塞尔中点法); 缓存在 shape._path2d */
export function buildPath2D(shape) {
  const pts = shape.points || [];
  const p = new Path2D();
  if (!pts.length) return p;
  const ox = Number(shape.x) || 0; const oy = Number(shape.y) || 0;
  p.moveTo(pts[0][0] + ox, pts[0][1] + oy);
  for (let i = 1; i < pts.length - 1; i++) {
    const mx = (pts[i - 1][0] + pts[i][0]) / 2 + ox;
    const my = (pts[i - 1][1] + pts[i][1]) / 2 + oy;
    p.quadraticCurveTo(pts[i - 1][0] + ox, pts[i - 1][1] + oy, mx, my);
  }
  if (pts.length > 1) {
    const last = pts[pts.length - 1];
    p.lineTo(last[0] + ox, last[1] + oy);
  }
  return p;
}

export function invalidatePathCache(shape) {
  shape._path2d = null;
}
