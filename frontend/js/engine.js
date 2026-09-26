/* ================================================================
   engine.js —— Canvas 渲染引擎

   **难点: 大画布渲染性能**
   - 三层画布: grid(点阵背景) / main(图形) / overlay(选择框、句柄、
     远程光标、框选)。移动光标/框选只重绘 overlay, 图形层零开销;
   - rAF 脏标记调度: 每层独立 dirty 位, 无变化不重绘;
   - 均匀网格空间索引(SpatialGrid)做视口裁剪: 每帧只遍历与可见
     区域相交的候选图形, 5000+ 图元仍可交互流畅;
   - Path2D 缓存: 手绘笔迹的路径对象构建一次, 移动时仅变换;
   - LOD: 缩放 < 0.35 不绘制文本, < 0.15 简化描边;
   - devicePixelRatio 适配, 高清屏不糊。
   ================================================================ */
import {
  SpatialGrid, bboxOf, bboxIntersects, buildPath2D, centerOf,
  edgeEndpoints, emptyBbox, expandBbox, hitTest, shapesBbox, wrapText,
} from './shapes.js';

const HANDLE_SIZE = 8;

/* roundRect 兼容垫片(旧内核) */
if (typeof CanvasRenderingContext2D !== 'undefined'
  && !CanvasRenderingContext2D.prototype.roundRect) {
  CanvasRenderingContext2D.prototype.roundRect = function roundRect(x, y, w, h, r) {
    const rr = Math.min(Number(r) || 0, w / 2, h / 2);
    this.moveTo(x + rr, y);
    this.arcTo(x + w, y, x + w, y + h, rr);
    this.arcTo(x + w, y + h, x, y + h, rr);
    this.arcTo(x, y + h, x, y, rr);
    this.arcTo(x, y, x + w, y, rr);
    this.closePath();
    return this;
  };
}

export function stripPrivate(shape) {
  const out = {};
  for (const [k, v] of Object.entries(shape)) {
    if (!k.startsWith('_')) out[k] = v;
  }
  return out;
}

export class BoardEngine {
  /**
   * @param {HTMLElement} container 画布容器(相对定位)
   * @param {object} opts
   *   background   画布底色(默认纸白)
   *   gridDots     是否画点阵(默认 true)
   *   readOnly     只读(回放/预览)
   *   mindLinks    是否绘制思维导图父子连线(默认 true)
   */
  constructor(container, opts = {}) {
    this.container = container;
    this.opts = {
      background: opts.background ?? '#fbfbf9',
      gridDots: opts.gridDots !== false,
      readOnly: !!opts.readOnly,
      mindLinks: opts.mindLinks !== false,
      accent: opts.accent ?? '#5b8ff9',
    };
    this.cam = { x: -60, y: -40, k: 1 };
    this.page = opts.page || 'editor';   // editor | mindmap(远程光标过滤用)
    this.shapes = new Map();
    this.gridIndex = new SpatialGrid(256);
    this.selection = new Set();
    this.remoteCursors = new Map();     // clientKey → {cursor,color,name,tool,selection,page}
    this.marquee = null;                // {x0,y0,x1,y1} 世界坐标
    this.hoverId = null;
    this.draftShape = null;             // 工具绘制中的预览图形
    this.draftEdge = null;              // {from,x1,y1,x2,y2} 连线预览
    this.editingId = null;              // 文本编辑中的图形(不绘制其文字)

    this._dirty = { grid: true, main: true, overlay: true };
    this._raf = null;
    this._dpr = 1;

    this._makeLayers();
    this._startLoop();
    this._observeResize();
  }

  /* ------------------------------------------------------------ 画布层 */
  _makeLayers() {
    const mk = (z, cls) => {
      const c = document.createElement('canvas');
      c.className = `wb-layer ${cls}`;
      c.style.cssText = `position:absolute;inset:0;width:100%;height:100%;z-index:${z}`;
      this.container.appendChild(c);
      return c;
    };
    this.container.style.position = this.container.style.position || 'relative';
    this.container.style.overflow = 'hidden';
    this.gridCanvas = mk(1, 'layer-grid');
    this.mainCanvas = mk(2, 'layer-main');
    this.overlayCanvas = mk(3, 'layer-overlay');
    this.gridCtx = this.gridCanvas.getContext('2d');
    this.ctx = this.mainCanvas.getContext('2d');
    this.octx = this.overlayCanvas.getContext('2d');
    this.topCanvas = this.overlayCanvas;    // 事件层(工具挂监听用)
  }

  _observeResize() {
    const ro = new ResizeObserver(() => this.resize());
    ro.observe(this.container);
    this.resize();
  }

  resize() {
    const rect = this.container.getBoundingClientRect();
    this.width = Math.max(1, rect.width);
    this.height = Math.max(1, rect.height);
    this._dpr = Math.min(window.devicePixelRatio || 1, 2.5);
    for (const c of [this.gridCanvas, this.mainCanvas, this.overlayCanvas]) {
      c.width = Math.round(this.width * this._dpr);
      c.height = Math.round(this.height * this._dpr);
    }
    this.markDirty();
  }

  /* ------------------------------------------------------------ 坐标变换 */
  worldToScreen(wx, wy) {
    return { x: (wx - this.cam.x) * this.cam.k, y: (wy - this.cam.y) * this.cam.k };
  }

  screenToWorld(sx, sy) {
    return { x: sx / this.cam.k + this.cam.x, y: sy / this.cam.k + this.cam.y };
  }

  visibleWorldRect() {
    const tl = this.screenToWorld(0, 0);
    const br = this.screenToWorld(this.width, this.height);
    return { x0: tl.x, y0: tl.y, x1: br.x, y1: br.y };
  }

  zoomAt(factor, sx, sy) {
    const k0 = this.cam.k;
    const k = Math.max(0.08, Math.min(8, k0 * factor));
    if (k === k0) return;
    const w = this.screenToWorld(sx, sy);
    this.cam.k = k;
    this.cam.x = w.x - sx / k;
    this.cam.y = w.y - sy / k;
    this.markDirty();
  }

  zoomTo(k, keepCenter = true) {
    if (keepCenter) {
      const c = this.screenToWorld(this.width / 2, this.height / 2);
      this.cam.k = k;
      this.cam.x = c.x - this.width / 2 / k;
      this.cam.y = c.y - this.height / 2 / k;
    } else this.cam.k = k;
    this.markDirty();
  }

  panBy(dxScreen, dyScreen) {
    this.cam.x -= dxScreen / this.cam.k;
    this.cam.y -= dyScreen / this.cam.k;
    this.markDirty('grid');
    this.markDirty('main');
    this.markDirty('overlay');
  }

  centerOn(wx, wy, k = null) {
    if (k) this.cam.k = k;
    this.cam.x = wx - this.width / 2 / this.cam.k;
    this.cam.y = wy - this.height / 2 / this.cam.k;
    this.markDirty();
  }

  fitToContent(padding = 80, maxK = 1.6) {
    const alive = [...this.shapes.values()].filter((s) => !s.deleted && s.kind !== 'ghost');
    if (!alive.length) { this.cam = { x: -this.width / 2 + 300, y: -this.height / 2 + 200, k: 1 }; this.markDirty(); return; }
    const b = shapesBbox(alive);
    const w = Math.max(1, b.x1 - b.x0); const h = Math.max(1, b.y1 - b.y0);
    const k = Math.min(maxK, Math.min((this.width - padding * 2) / w, (this.height - padding * 2) / h));
    this.cam.k = Math.max(0.08, k);
    this.cam.x = (b.x0 + b.x1) / 2 - this.width / 2 / this.cam.k;
    this.cam.y = (b.y0 + b.y1) / 2 - this.height / 2 / this.cam.k;
    this.markDirty();
  }

  /* ------------------------------------------------------------ 数据接入 */
  setShapesMap(map) {
    this.shapes = map;
    this.rebuildIndex();
    this.markDirty();
  }

  rebuildIndex() {
    this.gridIndex.rebuild([...this.shapes.values()].filter((s) => !s.deleted));
  }

  /** 远端/本地操作合并后调用: 增量刷新受影响图形索引 */
  onShapesChanged(ids) {
    if (!ids || !ids.size) { this.markDirty('main'); this.markDirty('overlay'); return; }
    for (const id of ids) {
      const shape = this.shapes.get(id);
      this.gridIndex.removeById(id);
      if (shape && !shape.deleted && shape.kind !== 'ghost') this.gridIndex.insert(shape);
    }
    this.markDirty('main');
    this.markDirty('overlay');
  }

  /* ------------------------------------------------------------ 命中测试 */
  hit(wx, wy, tol = 6) {
    const worldTol = tol / this.cam.k;
    const candidates = this.gridIndex.query({ x0: wx - worldTol * 3, y0: wy - worldTol * 3, x1: wx + worldTol * 3, y1: wy + worldTol * 3 });
    let best = null; let bestZ = -Infinity;
    for (const id of candidates) {
      const shape = this.shapes.get(id);
      if (!shape || shape.deleted || shape.kind === 'ghost') continue;
      if (shape.kind === 'edge') {
        const ends = edgeEndpoints(shape, this.shapes);
        if (!ends) continue;
        const d = this._distToEdgeCurve(wx, wy, ends);
        if (d <= worldTol + 4) {
          const z = Number(shape.z) || 0;
          if (z >= bestZ) { bestZ = z; best = shape; }
        }
        continue;
      }
      if (hitTest(shape, wx, wy, worldTol)) {
        const z = Number(shape.z) || 0;
        if (z >= bestZ) { bestZ = z; best = shape; }
      }
    }
    return best;
  }

  _distToEdgeCurve(wx, wy, { p1, p2 }) {
    // 与渲染一致的二次曲线近似: 采样 12 段折线求距离
    const mx = (p1.x + p2.x) / 2; const my = (p1.y + p2.y) / 2;
    const dx = p2.x - p1.x; const dy = p2.y - p1.y;
    const len = Math.hypot(dx, dy) || 1;
    const curve = Math.min(len * 0.12, 40);
    const cx = mx - dy / len * curve; const cy = my + dx / len * curve;
    let min = Infinity;
    let prev = p1;
    for (let i = 1; i <= 12; i++) {
      const t = i / 12;
      const x = (1 - t) * (1 - t) * p1.x + 2 * (1 - t) * t * cx + t * t * p2.x;
      const y = (1 - t) * (1 - t) * p1.y + 2 * (1 - t) * t * cy + t * t * p2.y;
      min = Math.min(min, this._distSeg(wx, wy, prev.x, prev.y, x, y));
      prev = { x, y };
    }
    return min;
  }

  _distSeg(px, py, ax, ay, bx, by) {
    const dx = bx - ax; const dy = by - ay;
    const len2 = dx * dx + dy * dy;
    let t = len2 ? ((px - ax) * dx + (py - ay) * dy) / len2 : 0;
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
  }

  shapesInRect(b) {
    const out = [];
    for (const id of this.gridIndex.query(b)) {
      const shape = this.shapes.get(id);
      if (shape && !shape.deleted && shape.kind !== 'ghost' && bboxIntersects(bboxOf(shape), b)) out.push(shape);
    }
    return out;
  }

  selectionBbox() {
    const shapes = [...this.selection].map((id) => this.shapes.get(id)).filter((s) => s && !s.deleted);
    if (!shapes.length) return null;
    return shapesBbox(shapes);
  }

  /* ------------------------------------------------------------ 渲染调度 */
  markDirty(layer = 'all') {
    if (layer === 'all' || layer === 'grid') this._dirty.grid = true;
    if (layer === 'all' || layer === 'main') this._dirty.main = true;
    if (layer === 'all' || layer === 'overlay') this._dirty.overlay = true;
  }

  _startLoop() {
    const loop = () => {
      this._raf = requestAnimationFrame(loop);
      if (this._dirty.grid) { this._dirty.grid = false; this._drawGrid(); }
      if (this._dirty.main) { this._dirty.main = false; this._drawMain(); }
      if (this._dirty.overlay) { this._dirty.overlay = false; this._drawOverlay(); }
    };
    this._raf = requestAnimationFrame(loop);
  }

  destroy() {
    if (this._raf) cancelAnimationFrame(this._raf);
    this.gridCanvas.remove(); this.mainCanvas.remove(); this.overlayCanvas.remove();
  }

  /* ------------------------------------------------------------ 背景层 */
  _drawGrid() {
    const ctx = this.gridCtx;
    const dpr = this._dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.width, this.height);
    ctx.fillStyle = this.opts.background;
    ctx.fillRect(0, 0, this.width, this.height);
    if (!this.opts.gridDots) return;
    let step = 24;
    while (step * this.cam.k < 10) step *= 5;
    while (step * this.cam.k > 90) step /= 5;
    const rect = this.visibleWorldRect();
    const startX = Math.floor(rect.x0 / step) * step;
    const startY = Math.floor(rect.y0 / step) * step;
    ctx.fillStyle = 'rgba(120,130,150,0.28)';
    const r = Math.max(0.7, Math.min(1.6, this.cam.k));
    for (let wx = startX; wx <= rect.x1; wx += step) {
      for (let wy = startY; wy <= rect.y1; wy += step) {
        const s = this.worldToScreen(wx, wy);
        ctx.beginPath();
        ctx.arc(s.x, s.y, r, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }

  /* ------------------------------------------------------------ 图形层 */
  _drawMain() {
    const ctx = this.ctx;
    const dpr = this._dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.width, this.height);
    const rect = this.visibleWorldRect();
    const visible = [];
    for (const id of this.gridIndex.query(rect)) {
      const shape = this.shapes.get(id);
      if (!shape || shape.deleted || shape.kind === 'ghost') continue;
      if (shape.kind === 'edge' || bboxIntersects(bboxOf(shape), rect, 8 / this.cam.k)) visible.push(shape);
    }
    visible.sort((a, b) => (Number(a.z) || 0) - (Number(b.z) || 0)
      || ((a.meta?.createdAt || 0) - (b.meta?.createdAt || 0))
      || String(a.id).localeCompare(String(b.id)));

    ctx.save();
    ctx.scale(1, 1);
    ctx.setTransform(dpr * this.cam.k, 0, 0, dpr * this.cam.k,
      dpr * (-this.cam.x * this.cam.k), dpr * (-this.cam.y * this.cam.k));

    const lodText = this.cam.k >= 0.32;
    const edges = this.opts.mindLinks ? visible.filter((s) => s.kind === 'edge') : [];
    if (this.opts.mindLinks) this._drawMindLinks(ctx, visible);
    for (const shape of visible) {
      if (shape.kind === 'edge') continue;             // 连线单独一层(垫底/置顶均可, 这里统一在节点前)
      this.drawShape(ctx, shape, lodText);
    }
    for (const shape of edges) this.drawShape(ctx, shape, lodText);
    if (this.draftShape) this.drawShape(ctx, this.draftShape, lodText, true);
    ctx.restore();

    if (this.draftEdge) this._drawDraftEdge(ctx, this.draftEdge);
  }

  _drawMindLinks(ctx, visible) {
    const vis = new Set(visible.map((s) => s.id));
    ctx.save();
    ctx.strokeStyle = '#9aa4b5';
    ctx.lineWidth = 2 / Math.max(this.cam.k, 0.4);
    ctx.beginPath();
    for (const shape of visible) {
      if (shape.kind !== 'mindnode' || !shape.parent) continue;
      const parent = this.shapes.get(shape.parent);
      if (!parent || parent.deleted || !vis.has(parent.id)) continue;
      if (parent.collapsed) continue;
      const pb = bboxOf(parent); const cb = bboxOf(shape);
      const px = pb.x1; const py = (pb.y0 + pb.y1) / 2;
      const cx = cb.x0; const cy = (cb.y0 + cb.y1) / 2;
      const mid = (px + cx) / 2;
      ctx.moveTo(px, py);
      ctx.bezierCurveTo(mid, py, mid, cy, cx, cy);
    }
    ctx.stroke();
    ctx.restore();
  }

  drawShape(ctx, shape, lodText = true, isDraft = false) {
    const kind = shape.kind;
    const x = Number(shape.x) || 0; const y = Number(shape.y) || 0;
    const w = Number(shape.w) || 0; const h = Number(shape.h) || 0;
    const stroke = shape.stroke || '#374151';
    const fill = shape.fill && shape.fill !== 'transparent' ? shape.fill : null;
    const sw = Number(shape.strokeWidth) ?? 2;
    const opacity = shape.opacity == null ? 1 : Number(shape.opacity);
    ctx.save();
    if (opacity < 1) ctx.globalAlpha = Math.max(0, Math.min(1, opacity));
    if (isDraft) ctx.globalAlpha = (ctx.globalAlpha || 1) * 0.75;
    if (shape.rotation) {
      const c = { x: x + w / 2, y: y + h / 2 };
      ctx.translate(c.x, c.y);
      ctx.rotate((Number(shape.rotation) * Math.PI) / 180);
      ctx.translate(-c.x, -c.y);
    }
    const dash = Array.isArray(shape.dash) && shape.dash.length ? shape.dash : null;
    if (dash) ctx.setLineDash(dash);

    switch (kind) {
      case 'rect':
      case 'note':
      case 'mindnode': {
        const r = Math.min(Number(shape.cornerRadius) || (kind === 'note' ? 4 : 8), Math.min(w, h) / 2);
        if (kind === 'note') { ctx.shadowColor = 'rgba(0,0,0,0.14)'; ctx.shadowBlur = 8; ctx.shadowOffsetY = 3; }
        this._roundRect(ctx, x, y, w, h, Math.max(0, r));
        if (fill) { ctx.fillStyle = fill; ctx.fill(); }
        ctx.shadowColor = 'transparent';
        if (sw > 0 && stroke !== 'transparent') { ctx.strokeStyle = stroke; ctx.lineWidth = sw; ctx.stroke(); }
        if (kind === 'mindnode' && shape.collapsed) this._collapseBadge(ctx, x, y, w, h);
        if (lodText && shape.id !== this.editingId) {
          this._drawWrappedText(ctx, shape, x + w / 2, y + h / 2, Math.max(16, w - 14), 'center');
        }
        break;
      }
      case 'ellipse': {
        ctx.beginPath();
        ctx.ellipse(x + w / 2, y + h / 2, Math.max(w / 2, 0.5), Math.max(h / 2, 0.5), 0, 0, Math.PI * 2);
        if (fill) { ctx.fillStyle = fill; ctx.fill(); }
        if (sw > 0 && stroke !== 'transparent') { ctx.strokeStyle = stroke; ctx.lineWidth = sw; ctx.stroke(); }
        if (lodText && shape.id !== this.editingId) this._drawWrappedText(ctx, shape, x + w / 2, y + h / 2, Math.max(16, w - 22), 'center');
        break;
      }
      case 'diamond': {
        ctx.beginPath();
        ctx.moveTo(x + w / 2, y); ctx.lineTo(x + w, y + h / 2);
        ctx.lineTo(x + w / 2, y + h); ctx.lineTo(x, y + h / 2);
        ctx.closePath();
        if (fill) { ctx.fillStyle = fill; ctx.fill(); }
        if (sw > 0 && stroke !== 'transparent') { ctx.strokeStyle = stroke; ctx.lineWidth = sw; ctx.stroke(); }
        if (lodText && shape.id !== this.editingId) this._drawWrappedText(ctx, shape, x + w / 2, y + h / 2, Math.max(16, w / 2), 'center');
        break;
      }
      case 'text': {
        if (lodText && shape.id !== this.editingId) {
          this._drawWrappedText(ctx, shape, x + w / 2, y + h / 2, Math.max(30, w), 'center');
        }
        break;
      }
      case 'path': {
        if (!shape._path2d || shape._pathDirty) { shape._path2d = buildPath2D(shape); shape._pathDirty = false; }
        ctx.strokeStyle = stroke;
        ctx.lineWidth = this.cam.k < 0.15 ? Math.max(sw, 2 / this.cam.k * 0.4) : sw;
        ctx.lineCap = 'round'; ctx.lineJoin = 'round';
        ctx.stroke(shape._path2d);
        break;
      }
      case 'line':
      case 'arrow': {
        const pts = shape.points || [];
        if (pts.length >= 2) {
          const a = pts[0]; const b = pts[pts.length - 1];
          ctx.strokeStyle = stroke; ctx.lineWidth = sw; ctx.lineCap = 'round';
          ctx.beginPath();
          ctx.moveTo(a[0] + x, a[1] + y);
          ctx.lineTo(b[0] + x, b[1] + y);
          ctx.stroke();
          if (kind === 'arrow') this._arrowHead(ctx, a[0] + x, a[1] + y, b[0] + x, b[1] + y, stroke, Math.max(sw * 2.6, 8));
        }
        break;
      }
      case 'edge': {
        const ends = edgeEndpoints(shape, this.shapes);
        if (!ends) break;
        const { p1, p2 } = ends;
        const mx = (p1.x + p2.x) / 2; const my = (p1.y + p2.y) / 2;
        const dx = p2.x - p1.x; const dy = p2.y - p1.y;
        const len = Math.hypot(dx, dy) || 1;
        const curve = Math.min(len * 0.12, 40);
        const cx = mx - dy / len * curve; const cy = my + dx / len * curve;
        ctx.strokeStyle = stroke; ctx.lineWidth = sw; ctx.lineCap = 'round';
        ctx.beginPath();
        ctx.moveTo(p1.x, p1.y);
        ctx.quadraticCurveTo(cx, cy, p2.x, p2.y);
        ctx.stroke();
        this._arrowHead(ctx, cx, cy, p2.x, p2.y, stroke, Math.max(sw * 2.8, 9));
        const label = shape.text || '';
        if (lodText && label.trim()) {
          ctx.font = `${Number(shape.fontSize) || 12}px "PingFang SC","Microsoft YaHei",sans-serif`;
          ctx.fillStyle = '#ffffffcc';
          const tw = ctx.measureText(label).width;
          ctx.fillRect(cx - tw / 2 - 4, cy - 18, tw + 8, 16);
          ctx.fillStyle = shape.textColor || '#4b5563';
          ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
          ctx.fillText(label, cx, cy - 10);
        }
        break;
      }
      default: break;
    }
    ctx.restore();
  }

  _collapseBadge(ctx, x, y, w, h) {
    const bx = x + w + 4; const by = y + h / 2;
    ctx.save();
    ctx.beginPath();
    ctx.arc(bx, by, 8, 0, Math.PI * 2);
    ctx.fillStyle = '#fff';
    ctx.strokeStyle = '#9aa4b5';
    ctx.lineWidth = 1.4;
    ctx.fill(); ctx.stroke();
    ctx.fillStyle = '#5f6b7f';
    ctx.font = 'bold 11px sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('+', bx, by + 0.5);
    ctx.restore();
  }

  _roundRect(ctx, x, y, w, h, r) {
    r = Math.max(0, Math.min(r, Math.min(w, h) / 2));
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  _arrowHead(ctx, fx, fy, tx, ty, color, size) {
    const ang = Math.atan2(ty - fy, tx - fx);
    ctx.save();
    ctx.translate(tx, ty);
    ctx.rotate(ang);
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.lineTo(-size, size * 0.42);
    ctx.lineTo(-size, -size * 0.42);
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
    ctx.restore();
  }

  _drawWrappedText(ctx, shape, cx, cy, maxW, align = 'center') {
    const text = shape.text || '';
    if (!text.trim()) return;
    const fs = Number(shape.fontSize) || 14;
    const weight = shape.fontWeight || 'normal';
    ctx.font = `${weight} ${fs}px "PingFang SC","Microsoft YaHei",sans-serif`;
    ctx.fillStyle = shape.textColor || (shape.kind === 'note' ? '#3a3a35' : '#1f2937');
    ctx.textAlign = align;
    ctx.textBaseline = 'middle';
    const lines = wrapText(text, fs, maxW);
    const lh = fs * 1.35;
    let y = cy - (lh * (lines.length - 1)) / 2;
    for (const line of lines) { ctx.fillText(line, cx, y, maxW + 8); y += lh; }
  }

  _drawDraftEdge(ctx, draft) {
    const p1 = this.worldToScreen(draft.x1, draft.y1);
    const p2 = this.worldToScreen(draft.x2, draft.y2);
    ctx.save();
    ctx.setTransform(this._dpr, 0, 0, this._dpr, 0, 0);
    ctx.strokeStyle = draft.color || this.opts.accent;
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.stroke();
    ctx.restore();
  }

  /* ------------------------------------------------------------ 覆盖层 */
  _drawOverlay() {
    const ctx = this.octx;
    const dpr = this._dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.width, this.height);

    // 远程用户的选择高亮(仅同页面模式)
    for (const remote of this.remoteCursors.values()) {
      if (!remote.selection?.length) continue;
      if (remote.page && this.page && remote.page !== this.page) continue;
      ctx.save();
      ctx.strokeStyle = `${remote.color}88`;
      ctx.lineWidth = 1.5;
      for (const id of remote.selection) {
        const shape = this.shapes.get(id);
        if (!shape || shape.deleted) continue;
        const b = bboxOf(shape);
        const s0 = this.worldToScreen(b.x0, b.y0);
        const s1 = this.worldToScreen(b.x1, b.y1);
        ctx.strokeRect(s0.x - 2, s0.y - 2, s1.x - s0.x + 4, s1.y - s0.y + 4);
      }
      ctx.restore();
    }

    // 本地选择框 + 句柄
    if (!this.opts.readOnly) this._drawSelection(ctx);

    // 框选矩形
    if (this.marquee) {
      const s0 = this.worldToScreen(this.marquee.x0, this.marquee.y0);
      const s1 = this.worldToScreen(this.marquee.x1, this.marquee.y1);
      ctx.save();
      ctx.fillStyle = `${this.opts.accent}18`;
      ctx.strokeStyle = this.opts.accent;
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.fillRect(s0.x, s0.y, s1.x - s0.x, s1.y - s0.y);
      ctx.strokeRect(s0.x, s0.y, s1.x - s0.x, s1.y - s0.y);
      ctx.restore();
    }

    // 悬停提示
    if (this.hoverId && !this.selection.has(this.hoverId) && !this.opts.readOnly) {
      const shape = this.shapes.get(this.hoverId);
      if (shape && !shape.deleted) {
        const b = bboxOf(shape);
        const s0 = this.worldToScreen(b.x0, b.y0);
        const s1 = this.worldToScreen(b.x1, b.y1);
        ctx.save();
        ctx.strokeStyle = `${this.opts.accent}66`;
        ctx.lineWidth = 1.5;
        ctx.strokeRect(s0.x - 1.5, s0.y - 1.5, s1.x - s0.x + 3, s1.y - s0.y + 3);
        ctx.restore();
      }
    }

    this._drawRemoteCursors(ctx);
  }

  _drawSelection(ctx) {
    const b = this.selectionBbox();
    if (!b) return;
    const s0 = this.worldToScreen(b.x0, b.y0);
    const s1 = this.worldToScreen(b.x1, b.y1);
    ctx.save();
    ctx.strokeStyle = this.opts.accent;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([]);
    ctx.strokeRect(s0.x - 3, s0.y - 3, s1.x - s0.x + 6, s1.y - s0.y + 6);
    if (this.selection.size === 1) {
      const positions = this.handlePositions(b);
      ctx.fillStyle = '#fff';
      for (const p of positions) {
        ctx.beginPath();
        ctx.rect(p.sx - HANDLE_SIZE / 2, p.sy - HANDLE_SIZE / 2, HANDLE_SIZE, HANDLE_SIZE);
        ctx.fill();
        ctx.strokeStyle = this.opts.accent;
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
    }
    // 尺寸标注
    const wWorld = Math.round(b.x1 - b.x0); const hWorld = Math.round(b.y1 - b.y0);
    ctx.fillStyle = this.opts.accent;
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'center';
    const label = this.selection.size > 1 ? `${this.selection.size} 个图形` : `${wWorld} × ${hWorld}`;
    const tw = ctx.measureText(label).width + 12;
    ctx.beginPath();
    ctx.roundRect(s0.x + (s1.x - s0.x) / 2 - tw / 2, s1.y + 8, tw, 17, 4);
    ctx.fill();
    ctx.fillStyle = '#fff';
    ctx.textBaseline = 'middle';
    ctx.fillText(label, s0.x + (s1.x - s0.x) / 2, s1.y + 17);
    ctx.restore();
  }

  handlePositions(b) {
    const pts = [
      ['nw', b.x0, b.y0], ['n', (b.x0 + b.x1) / 2, b.y0], ['ne', b.x1, b.y0],
      ['e', b.x1, (b.y0 + b.y1) / 2], ['se', b.x1, b.y1],
      ['s', (b.x0 + b.x1) / 2, b.y1], ['sw', b.x0, b.y1], ['w', b.x0, (b.y0 + b.y1) / 2],
    ];
    return pts.map(([dir, wx, wy]) => {
      const s = this.worldToScreen(wx, wy);
      return { dir, sx: s.x, sy: s.y, wx, wy };
    });
  }

  /** 屏幕坐标命中的缩放手柄 */
  hitHandle(sx, sy) {
    const b = this.selectionBbox();
    if (!b || this.selection.size !== 1) return null;
    for (const p of this.handlePositions(b)) {
      if (Math.abs(sx - p.sx) <= HANDLE_SIZE && Math.abs(sy - p.sy) <= HANDLE_SIZE) return p;
    }
    return null;
  }

  _drawRemoteCursors(ctx) {
    ctx.save();
    for (const remote of this.remoteCursors.values()) {
      if (!remote.cursor || remote.page !== this.page) continue;
      const s = this.worldToScreen(remote.cursor.x, remote.cursor.y);
      if (s.x < -40 || s.y < -40 || s.x > this.width + 40 || s.y > this.height + 40) continue;
      ctx.save();
      ctx.translate(s.x, s.y);
      ctx.fillStyle = remote.color || '#888';
      ctx.beginPath();
      ctx.moveTo(0, 0); ctx.lineTo(0, 17); ctx.lineTo(4.6, 12.6); ctx.lineTo(8, 20);
      ctx.lineTo(11, 18.6); ctx.lineTo(7.6, 11.4); ctx.lineTo(13, 10.6);
      ctx.closePath();
      ctx.fill();
      ctx.strokeStyle = '#ffffffaa'; ctx.lineWidth = 1; ctx.stroke();
      const name = remote.name || remote.user || '?';
      ctx.font = '11px "PingFang SC",sans-serif';
      const tw = ctx.measureText(name).width + 12;
      ctx.beginPath();
      ctx.roundRect(12, 18, tw, 17, 5);
      ctx.fill();
      ctx.fillStyle = '#fff';
      ctx.textBaseline = 'middle';
      ctx.fillText(name, 18, 27);
      ctx.restore();
    }
    ctx.restore();
  }

  /* ------------------------------------------------------------ 远程光标管理 */
  updateRemoteCursor(key, data) {
    const prev = this.remoteCursors.get(key) || {};
    this.remoteCursors.set(key, { ...prev, ...data });
    this.markDirty('overlay');
  }

  removeRemoteCursor(key) {
    if (this.remoteCursors.delete(key)) this.markDirty('overlay');
  }

  clearRemoteCursors() {
    this.remoteCursors.clear();
    this.markDirty('overlay');
  }

  /* ------------------------------------------------------------ 小地图 */
  drawMinimap(canvas, pad = 8) {
    const ctx = canvas.getContext('2d');
    const w = canvas.width; const h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = getComputedStyle(document.body).getPropertyValue('--bg-elevated') || '#222';
    ctx.fillRect(0, 0, w, h);
    const alive = [...this.shapes.values()].filter((s) => !s.deleted && s.kind !== 'ghost');
    if (!alive.length) return;
    let b = shapesBbox(alive);
    const view = this.visibleWorldRect();
    b = expandBbox(b, view);
    const bw = Math.max(1, b.x1 - b.x0); const bh = Math.max(1, b.y1 - b.y0);
    const k = Math.min((w - pad * 2) / bw, (h - pad * 2) / bh);
    const ox = pad + (w - pad * 2 - bw * k) / 2 - b.x0 * k;
    const oy = pad + (h - pad * 2 - bh * k) / 2 - b.y0 * k;
    for (const shape of alive) {
      const sb = bboxOf(shape);
      ctx.fillStyle = shape.kind === 'mindnode' ? (shape.fill || '#61c0a8')
        : shape.kind === 'edge' ? 'transparent' : (shape.stroke && shape.stroke !== 'transparent' ? shape.stroke : '#8a93a5');
      if (shape.kind === 'path' || shape.kind === 'edge') ctx.fillStyle = '#8a93a5';
      ctx.globalAlpha = 0.75;
      ctx.fillRect(ox + sb.x0 * k, oy + sb.y0 * k,
        Math.max(1.5, (sb.x1 - sb.x0) * k), Math.max(1.5, (sb.y1 - sb.y0) * k));
    }
    ctx.globalAlpha = 1;
    ctx.strokeStyle = '#5b8ff9';
    ctx.lineWidth = 1.4;
    ctx.strokeRect(ox + view.x0 * k, oy + view.y0 * k,
      Math.max(2, (view.x1 - view.x0) * k), Math.max(2, (view.y1 - view.y0) * k));
  }

  /* ------------------------------------------------------------ 静态渲染(缩略图/导出) */
  /**
   * 把一组图形渲染到独立 canvas(不走相机): 缩略图、模板预览、PNG 导出。
   * @returns canvas
   */
  static renderStatic(canvas, shapesIterable, opts = {}) {
    const {
      background = '#fbfbf9', padding = 30, scale = 1, maxW = null, maxH = null,
      mindLinks = true, grid = false,
    } = opts;
    const shapes = [...shapesIterable].filter((s) => s && !s.deleted && s.kind !== 'ghost');
    const ctx = canvas.getContext('2d');
    if (!shapes.length) {
      canvas.width = maxW || 480; canvas.height = maxH || 300;
      ctx.fillStyle = background;
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = '#b6bdca';
      ctx.font = '15px sans-serif';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText('空白板', canvas.width / 2, canvas.height / 2);
      return canvas;
    }
    const b = shapesBbox(shapes);
    const bw = Math.max(1, b.x1 - b.x0) + padding * 2;
    const bh = Math.max(1, b.y1 - b.y0) + padding * 2;
    let k = scale;
    if (maxW) k = Math.min(k, maxW / bw);
    if (maxH) k = Math.min(k, maxH / bh);
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.max(1, Math.round(bw * k * dpr));
    canvas.height = Math.max(1, Math.round(bh * k * dpr));
    canvas.style.width = `${Math.round(bw * k)}px`;
    canvas.style.height = `${Math.round(bh * k)}px`;
    ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * k * (padding - b.x0), dpr * k * (padding - b.y0));
    ctx.fillStyle = background;
    ctx.fillRect(b.x0 - padding, b.y0 - padding, bw, bh);
    if (grid) {
      ctx.fillStyle = 'rgba(120,130,150,0.25)';
      for (let wx = Math.floor(b.x0 / 24) * 24; wx < b.x1 + padding; wx += 24) {
        for (let wy = Math.floor(b.y0 / 24) * 24; wy < b.y1 + padding; wy += 24) {
          ctx.beginPath(); ctx.arc(wx, wy, 1, 0, Math.PI * 2); ctx.fill();
        }
      }
    }
    const fakeEngine = {
      cam: { k: 1, x: 0, y: 0 },
      shapes: new Map(shapes.map((s) => [s.id, s])),
      editingId: null,
      drawShape: BoardEngine.prototype.drawShape,
      _roundRect: BoardEngine.prototype._roundRect,
      _arrowHead: BoardEngine.prototype._arrowHead,
      _drawWrappedText: BoardEngine.prototype._drawWrappedText,
      _collapseBadge: BoardEngine.prototype._collapseBadge,
    };
    const sorted = [...shapes].sort((a, b2) => (Number(a.z) || 0) - (Number(b2.z) || 0));
    if (mindLinks) {
      ctx.save();
      ctx.strokeStyle = '#9aa4b5'; ctx.lineWidth = 2;
      ctx.beginPath();
      for (const shape of sorted) {
        if (shape.kind !== 'mindnode' || !shape.parent) continue;
        const parent = fakeEngine.shapes.get(shape.parent);
        if (!parent || parent.deleted) continue;
        const pb = bboxOf(parent); const cb = bboxOf(shape);
        const px = pb.x1; const py = (pb.y0 + pb.y1) / 2;
        const cx = cb.x0; const cy = (cb.y0 + cb.y1) / 2;
        const mid = (px + cx) / 2;
        ctx.moveTo(px, py);
        ctx.bezierCurveTo(mid, py, mid, cy, cx, cy);
      }
      ctx.stroke();
      ctx.restore();
    }
    for (const shape of sorted.filter((s) => s.kind !== 'edge')) fakeEngine.drawShape(ctx, shape, true);
    for (const shape of sorted.filter((s) => s.kind === 'edge')) fakeEngine.drawShape(ctx, shape, true);
    return canvas;
  }
}

export { HANDLE_SIZE };
