/* ================================================================
   tools.js —— 编辑器工具状态机

   工具: select / pan / pen / line / arrow / rect / ellipse / diamond /
        note / text / edge / eraser
   交互要点:
   - 拖动图形: 期间以节流 move 增量实时广播(协作者看到实时拖动),
     pointerup 时把「累计位移」压入撤销栈(逆操作=反向增量);
   - 缩放图形: 本地实时预览, pointerup 一次性提交 set_props
     (x/y/w/h 的 LWW 写入, 撤销=写回旧值);
   - 手绘: add_shape 起笔 → 节流 path_extend 续笔 → 收笔抽稀;
   - 双击: 便签/文本/图形 进入内联文本编辑(textarea 覆盖画布);
   - 键盘: Delete 删除, Ctrl+Z/Y 撤销重做, Ctrl+C/V/D 复制粘贴,
     方向键微移, Ctrl+] / Ctrl+[ 层级调整。
   ================================================================ */
import {
  bboxOf, centerOf, createShape, nextZ, syncZCounter, uidShape,
} from './shapes.js';

function stripPrivate(shape) {
  const out = {};
  for (const [k, v] of Object.entries(shape)) if (!k.startsWith('_')) out[k] = v;
  return out;
}

const TOOL_KEYS = {
  v: 'select', h: 'pan', p: 'pen', l: 'line', a: 'arrow', r: 'rect',
  o: 'ellipse', d: 'diamond', n: 'note', t: 'text', c: 'edge', x: 'eraser',
};

export class ToolManager {
  /**
   * @param {object} deps
   *   engine   BoardEngine
   *   crdt     CrdtClient
   *   socket   BoardSocket(可空, 只读模式)
   *   shapes   Map(与 engine.shapes 同一引用)
   *   opts     { readOnly, snap, onToolChange, onPresence, onSelectionChange,
   *              onTextEdit, style }
   */
  constructor(deps) {
    this.engine = deps.engine;
    this.crdt = deps.crdt;
    this.socket = deps.socket || null;
    this.shapes = deps.shapes;
    this.opts = deps.opts || {};
    this.tool = 'select';
    this.style = this.opts.style || {
      stroke: '#374151', fill: '#ffffff', strokeWidth: 2, fontSize: 14,
    };
    this.readOnly = !!this.opts.readOnly;
    this.snap = !!this.opts.snap;
    this.snapSize = 12;

    this._drag = null;          // 当前手势状态机
    this._spaceDown = false;
    this._clipboard = null;
    this._edgeFirst = null;     // 连线工具的起点图形
    this._editor = null;        // 内联文本编辑 textarea
    this._presenceThrottle = 0;

    this._bindPointer();
    this._bindWheel();
    if (!this.opts.noKeyboard) this._bindKeyboard();
  }

  setTool(tool) {
    if (this.tool === tool) return;
    this.tool = tool;
    this._edgeFirst = null;
    this.engine.topCanvas.style.cursor = this.cursorFor(tool);
    if (this.opts.onToolChange) this.opts.onToolChange(tool);
    this._emitPresence();
  }

  cursorFor(tool) {
    if (this._spaceDown || tool === 'pan') return 'grab';
    if (tool === 'select') return 'default';
    if (tool === 'text') return 'text';
    if (tool === 'eraser') return 'cell';
    return 'crosshair';
  }

  setStyle(patch) {
    Object.assign(this.style, patch);
    // 选中图形时同步改样式
    if (this.engine.selection.size && !this.readOnly) {
      this.applyStyleToSelection(patch);
    }
  }

  applyStyleToSelection(props) {
    if (this.readOnly) return;
    const ops = [];
    for (const id of this.engine.selection) {
      const shape = this.shapes.get(id);
      if (!shape || shape.deleted) continue;
      const clean = {};
      for (const [k, v] of Object.entries(props)) {
        if (shape[k] !== v) clean[k] = v;
      }
      if (Object.keys(clean).length) ops.push(this.crdt.setProps(id, clean));
    }
    if (ops.length) this.crdt.commit(this.shapes, ops, { label: '样式' });
    this.engine.markDirty('main');
  }

  /* ------------------------------------------------------------ 指针事件 */
  _bindPointer() {
    const canvas = this.engine.topCanvas;
    canvas.addEventListener('pointerdown', (e) => this._onPointerDown(e));
    window.addEventListener('pointermove', (e) => this._onPointerMove(e));
    window.addEventListener('pointerup', (e) => this._onPointerUp(e));
    canvas.addEventListener('dblclick', (e) => this._onDblClick(e));
    canvas.addEventListener('contextmenu', (e) => this._onContextMenu(e));
    canvas.addEventListener('pointerleave', () => {
      this.engine.hoverId = null;
      this.engine.markDirty('overlay');
    });
  }

  _pos(e) {
    const rect = this.engine.topCanvas.getBoundingClientRect();
    return { sx: e.clientX - rect.left, sy: e.clientY - rect.top };
  }

  _onPointerDown(e) {
    if (e.button === 1 || this._spaceDown) {          // 中键/空格 → 平移
      this._drag = { mode: 'pan', lastX: e.clientX, lastY: e.clientY };
      this.engine.topCanvas.setPointerCapture(e.pointerId);
      this.engine.topCanvas.style.cursor = 'grabbing';
      e.preventDefault();
      return;
    }
    if (e.button !== 0) return;
    if (this.readOnly) {
      this._drag = { mode: 'pan', lastX: e.clientX, lastY: e.clientY };
      this.engine.topCanvas.setPointerCapture(e.pointerId);
      return;
    }
    const { sx, sy } = this._pos(e);
    const w = this.engine.screenToWorld(sx, sy);

    // 缩放手柄优先
    const handle = this.engine.hitHandle(sx, sy);
    if (handle && this.tool === 'select') {
      const id = [...this.engine.selection][0];
      const shape = this.shapes.get(id);
      this._drag = {
        mode: 'resize', handle, id,
        before: { x: shape.x, y: shape.y, w: shape.w, h: shape.h },
        moved: false,
      };
      this.engine.topCanvas.setPointerCapture(e.pointerId);
      return;
    }

    const hitShape = this.engine.hit(w.x, w.y);

    switch (this.tool) {
      case 'select': this._selectPointerDown(e, w, hitShape); break;
      case 'pan':
        this._drag = { mode: 'pan', lastX: e.clientX, lastY: e.clientY };
        this.engine.topCanvas.setPointerCapture(e.pointerId);
        this.engine.topCanvas.style.cursor = 'grabbing';
        break;
      case 'pen': this._penDown(w); break;
      case 'line': case 'arrow': this._lineDown(w); break;
      case 'rect': case 'ellipse': case 'diamond': case 'note': this._shapeDown(w); break;
      case 'text': this._textDown(w); break;
      case 'edge': this._edgeDown(hitShape, w); break;
      case 'eraser': this._eraserDown(hitShape); break;
      default: break;
    }
    if (this._drag) this.engine.topCanvas.setPointerCapture(e.pointerId);
  }

  _selectPointerDown(e, w, hitShape) {
    if (hitShape) {
      if (e.shiftKey) {
        if (this.engine.selection.has(hitShape.id)) this.engine.selection.delete(hitShape.id);
        else this.engine.selection.add(hitShape.id);
      } else if (!this.engine.selection.has(hitShape.id)) {
        this.engine.selection = new Set([hitShape.id]);
      }
      this._notifySelection();
      this._drag = {
        mode: 'move',
        startW: w,
        accum: { dx: 0, dy: 0 },        // 已广播的累计位移
        sent: { dx: 0, dy: 0 },         // 已发送的位移
        lastSend: 0,
        ids: [...this.engine.selection],
        moved: false,
      };
      this.engine.markDirty('overlay');
      return;
    }
    if (!e.shiftKey) { this.engine.selection = new Set(); this._notifySelection(); }
    this._drag = { mode: 'marquee', startW: w, additive: e.shiftKey,
      base: new Set(this.engine.selection) };
    this.engine.marquee = { x0: w.x, y0: w.y, x1: w.x, y1: w.y };
    this.engine.markDirty('overlay');
  }

  _penDown(w) {
    const x = this.snap ? Math.round(w.x / this.snapSize) * this.snapSize : w.x;
    const y = this.snap ? Math.round(w.y / this.snapSize) * this.snapSize : w.y;
    const shape = createShape('path', {
      x, y, points: [[0, 0]],
      stroke: this.style.stroke, strokeWidth: this.style.strokeWidth,
      author: this.opts.me?.username || '', z: nextZ(),
    });
    shape._path2d = null;
    this.shapes.set(shape.id, shape);
    this.engine.gridIndex.insert(shape);
    this.crdt.send(this.shapes, [this.crdt.addShape(stripPrivate(shape))]);
    this._drag = { mode: 'pen', id: shape.id, lastExtend: 0, pendingPts: [], lastPt: [0, 0] };
    this.engine.markDirty('main');
  }

  _lineDown(w) {
    this._drag = {
      mode: 'line', kind: this.tool,
      start: w,
    };
    this.engine.draftShape = createShape(this.tool, {
      x: w.x, y: w.y, points: [[0, 0], [0, 0]],
      stroke: this.style.stroke, strokeWidth: this.style.strokeWidth, z: nextZ(),
    });
    this.engine.markDirty('main');
  }

  _shapeDown(w) {
    this._drag = { mode: 'shape', kind: this.tool, start: w };
    this.engine.draftShape = createShape(this.tool, {
      x: w.x, y: w.y, w: 0, h: 0,
      stroke: this.style.stroke, fill: this.tool === 'note' ? undefined : this.style.fill,
      strokeWidth: this.style.strokeWidth, fontSize: this.style.fontSize,
      z: nextZ(),
    });
    this.engine.markDirty('main');
  }

  _textDown(w) {
    const shape = createShape('text', {
      x: w.x - 130, y: w.y - 20, w: 260, h: 40, text: '',
      fontSize: Math.max(14, this.style.fontSize), z: nextZ(),
      textColor: '#1f2937',
      author: this.opts.me?.username || '',
    });
    this.shapes.set(shape.id, shape);
    this.engine.gridIndex.insert(shape);
    this.crdt.commit(this.shapes, [this.crdt.addShape(stripPrivate(shape))], { label: '添加文本' });
    this.engine.selection = new Set([shape.id]);
    this._notifySelection();
    this.openTextEditor(shape.id);
    this.setTool('select');
    this.engine.markDirty();
  }

  _edgeDown(hitShape, w) {
    if (!hitShape || hitShape.kind === 'edge') return;
    if (!this._edgeFirst) {
      this._edgeFirst = hitShape;
      this.engine.draftEdge = { x1: w.x, y1: w.y, x2: w.x, y2: w.y, color: this.style.stroke };
      this.engine.markDirty('main');
      return;
    }
    if (hitShape.id !== this._edgeFirst.id) {
      const edge = createShape('edge', {
        from: this._edgeFirst.id, to: hitShape.id,
        stroke: this.style.stroke, strokeWidth: this.style.strokeWidth,
        author: this.opts.me?.username || '', z: nextZ(),
      });
      this.shapes.set(edge.id, edge);
      this.crdt.commit(this.shapes, [this.crdt.addShape(stripPrivate(edge))], { label: '连线' });
      this.engine.markDirty('main');
    }
    this._edgeFirst = null;
    this.engine.draftEdge = null;
    this.engine.markDirty('main');
  }

  _eraserDown(hitShape) {
    this._drag = { mode: 'eraser', erased: [] };
    if (hitShape) this._eraseOne(hitShape);
  }

  _eraseOne(shape) {
    if (!shape || shape.deleted || shape.kind === 'ghost') return;
    if (this._drag?.mode === 'eraser') this._drag.erased.push(shape.id);
    shape.deleted = true;
    this.engine.gridIndex.remove(shape);
    this.engine.markDirty('main');
  }

  /* ------------------------------------------------------------ 移动中 */
  _onPointerMove(e) {
    const { sx, sy } = this._pos(e);
    const w = this.engine.screenToWorld(sx, sy);
    this._throttledPresence(w);

    if (!this._drag) {
      if (!this.readOnly && this.tool === 'select') {
        const handle = this.engine.hitHandle(sx, sy);
        if (handle) {
          const cursors = { nw: 'nwse-resize', se: 'nwse-resize', ne: 'nesw-resize', sw: 'nesw-resize', n: 'ns-resize', s: 'ns-resize', e: 'ew-resize', w: 'ew-resize' };
          this.engine.topCanvas.style.cursor = cursors[handle.dir] || 'default';
        } else {
          const hitShape = this.engine.hit(w.x, w.y);
          const hoverId = hitShape?.id || null;
          if (hoverId !== this.engine.hoverId) {
            this.engine.hoverId = hoverId;
            this.engine.markDirty('overlay');
          }
          this.engine.topCanvas.style.cursor = hitShape ? 'move' : this.cursorFor(this.tool);
        }
      }
      if (this._edgeFirst) {
        this.engine.draftEdge = {
          x1: centerOf(this._edgeFirst).x, y1: centerOf(this._edgeFirst).y,
          x2: w.x, y2: w.y, color: this.style.stroke,
        };
        this.engine.markDirty('main');
      }
      return;
    }

    const drag = this._drag;
    switch (drag.mode) {
      case 'pan': {
        const dx = e.clientX - drag.lastX; const dy = e.clientY - drag.lastY;
        drag.lastX = e.clientX; drag.lastY = e.clientY;
        this.engine.panBy(dx, dy);
        break;
      }
      case 'marquee': {
        this.engine.marquee = {
          x0: Math.min(drag.startW.x, w.x), y0: Math.min(drag.startW.y, w.y),
          x1: Math.max(drag.startW.x, w.x), y1: Math.max(drag.startW.y, w.y),
        };
        const inside = this.engine.shapesInRect(this.engine.marquee);
        const sel = drag.additive ? new Set(drag.base) : new Set();
        inside.forEach((s) => sel.add(s.id));
        this.engine.selection = sel;
        this.engine.markDirty('overlay');
        break;
      }
      case 'move': {
        const totalDx = w.x - drag.startW.x;
        const totalDy = w.y - drag.startW.y;
        const stepDx = totalDx - drag.accum.dx;
        const stepDy = totalDy - drag.accum.dy;
        if (!drag.moved && Math.hypot(totalDx, totalDy) * this.engine.cam.k < 3) break;
        drag.moved = true;
        // 本地立即更新(乐观)
        for (const id of drag.ids) {
          const shape = this.shapes.get(id);
          if (shape && !shape.deleted) {
            shape.x += stepDx; shape.y += stepDy;
            this.engine.gridIndex.refresh(shape);
          }
        }
        drag.accum = { dx: totalDx, dy: totalDy };
        // 节流广播增量(协作实时拖动)
        const now = performance.now();
        if (now - drag.lastSend > 70) {
          const dx = drag.accum.dx - drag.sent.dx;
          const dy = drag.accum.dy - drag.sent.dy;
          if (Math.abs(dx) > 0.01 || Math.abs(dy) > 0.01) {
            const ops = drag.ids.map((id) => this.crdt.move(id, dx, dy));
            this._sendRaw(ops);
            drag.sent = { dx: drag.accum.dx, dy: drag.accum.dy };
            drag.lastSend = now;
          }
        }
        this.engine.markDirty('main');
        this.engine.markDirty('overlay');
        break;
      }
      case 'resize': {
        const shape = this.shapes.get(drag.id);
        if (!shape) break;
        drag.moved = true;
        const b = drag.before;
        const dir = drag.handle.dir;
        let { x, y, w: ww, h: hh } = b;
        const wx = w.x; const wy = w.y;
        if (dir.includes('w')) { x = Math.min(wx, b.x + b.w - 8); ww = b.x + b.w - x; }
        if (dir.includes('e')) { ww = Math.max(8, wx - b.x); }
        if (dir.includes('n')) { y = Math.min(wy, b.y + b.h - 8); hh = b.y + b.h - y; }
        if (dir.includes('s')) { hh = Math.max(8, wy - b.y); }
        shape.x = x; shape.y = y; shape.w = ww; shape.h = hh;
        shape._pathDirty = true;
        this.engine.gridIndex.refresh(shape);
        this.engine.markDirty('main');
        this.engine.markDirty('overlay');
        break;
      }
      case 'pen': {
        const shape = this.shapes.get(drag.id);
        if (!shape) break;
        const rel = [w.x - shape.x, w.y - shape.y];
        if (Math.hypot(rel[0] - drag.lastPt[0], rel[1] - drag.lastPt[1]) < 2.2) break;
        drag.lastPt = rel;
        shape.points.push(rel);
        shape._pathDirty = true;
        drag.pendingPts.push(rel);
        const now = performance.now();
        if (now - drag.lastExtend > 90 && drag.pendingPts.length) {
          this._sendRaw([this.crdt.pathExtend(shape.id, drag.pendingPts)]);
          drag.pendingPts = [];
          drag.lastExtend = now;
        }
        this.engine.markDirty('main');
        break;
      }
      case 'line': {
        const draft = this.engine.draftShape;
        if (!draft) break;
        const pts = draft.points;
        pts[1] = [w.x - draft.x, w.y - draft.y];
        drag.moved = true;
        this.engine.markDirty('main');
        break;
      }
      case 'shape': {
        const draft = this.engine.draftShape;
        if (!draft) break;
        draft.x = Math.min(drag.start.x, w.x);
        draft.y = Math.min(drag.start.y, w.y);
        draft.w = Math.abs(w.x - drag.start.x);
        draft.h = Math.abs(w.y - drag.start.y);
        if (e.shiftKey) {                     // 正方形/正圆
          const size = Math.max(draft.w, draft.h);
          draft.w = size; draft.h = size;
        }
        drag.moved = true;
        this.engine.markDirty('main');
        break;
      }
      case 'eraser': {
        const hitShape = this.engine.hit(w.x, w.y);
        if (hitShape) this._eraseOne(hitShape);
        break;
      }
      default: break;
    }
  }

  _onPointerUp(e) {
    const drag = this._drag;
    this._drag = null;
    this.engine.topCanvas.style.cursor = this.cursorFor(this.tool);
    if (!drag) return;

    switch (drag.mode) {
      case 'marquee':
        this.engine.marquee = null;
        this._notifySelection();
        this.engine.markDirty('overlay');
        break;
      case 'move':
        if (drag.moved) {
          // 补发最后一段未广播的位移
          const dx = drag.accum.dx - drag.sent.dx;
          const dy = drag.accum.dy - drag.sent.dy;
          if (Math.abs(dx) > 0.01 || Math.abs(dy) > 0.01) {
            this._sendRaw(drag.ids.map((id) => this.crdt.move(id, dx, dy)));
          }
          // 撤销栈: 一步撤销整个拖动(逆操作 = 每个图形的反向累计位移)
          const undoOps = drag.ids.map((id) => ({ type: 'move', id, dx: -drag.accum.dx, dy: -drag.accum.dy }));
          const redoOps = drag.ids.map((id) => ({ type: 'move', id, dx: drag.accum.dx, dy: drag.accum.dy }));
          this.crdt.undoStack.push({ undoOps, redoOps, label: '移动' });
          this.crdt.redoStack.length = 0;
          this.engine.markDirty('overlay');
        }
        break;
      case 'resize': {
        const shape = this.shapes.get(drag.id);
        if (shape && drag.moved) {
          const b = drag.before;
          const props = { x: shape.x, y: shape.y, w: shape.w, h: shape.h };
          const semantic = { type: 'set_props', id: drag.id, props };
          const inverse = { type: 'set_props', id: drag.id, props: { x: b.x, y: b.y, w: b.w, h: b.h } };
          // 单次签发: 本地合并与网络发送使用同一个操作对象, 保证字段时钟一致
          this.crdt.send(this.shapes, [this.crdt.setProps(drag.id, props)]);
          this.crdt.undoStack.push({ undoOps: [inverse], redoOps: [semantic], label: '缩放' });
          this.crdt.redoStack.length = 0;
        }
        this.engine.markDirty('overlay');
        break;
      }
      case 'pen': {
        const shape = this.shapes.get(drag.id);
        if (shape) {
          if (drag.pendingPts.length) {
            this._sendRaw([this.crdt.pathExtend(shape.id, drag.pendingPts)]);
          }
          const fullShape = stripPrivate(shape);
          const undoOps = [{ type: 'delete_shape', id: shape.id }];
          const redoOps = [{ type: 'add_shape', shape: fullShape }];
          this.crdt.undoStack.push({ undoOps, redoOps, label: '手绘' });
          this.crdt.redoStack.length = 0;
          this.engine.selection = new Set([shape.id]);
          this._notifySelection();
        }
        this.engine.markDirty('main');
        break;
      }
      case 'line': {
        const draft = this.engine.draftShape;
        this.engine.draftShape = null;
        if (draft) {
          if (drag.moved) {
            const shape = createShape(draft.kind, {
              ...stripPrivate(draft), id: uidShape(),
              author: this.opts.me?.username || '',
            });
            this.shapes.set(shape.id, shape);
            this.engine.gridIndex.insert(shape);
            this.crdt.commit(this.shapes, [this.crdt.addShape(stripPrivate(shape))], { label: draft.kind === 'arrow' ? '箭头' : '直线' });
          }
        }
        this.engine.markDirty();
        break;
      }
      case 'shape': {
        const draft = this.engine.draftShape;
        this.engine.draftShape = null;
        if (draft) {
          let { x, y, w, h } = draft;
          if (w < 6 || h < 6) {                  // 单击 = 默认尺寸
            const defaults = { rect: [160, 100], ellipse: [150, 90], diamond: [160, 100], note: [190, 120] };
            [w, h] = defaults[draft.kind] || [150, 100];
            x -= w / 2; y -= h / 2;
          }
          const shape = createShape(draft.kind, {
            ...stripPrivate(draft), x, y, w, h, id: uidShape(),
            author: this.opts.me?.username || '',
          });
          this.shapes.set(shape.id, shape);
          this.engine.gridIndex.insert(shape);
          this.crdt.commit(this.shapes, [this.crdt.addShape(stripPrivate(shape))], { label: '图形' });
          this.engine.selection = new Set([shape.id]);
          this._notifySelection();
          if (draft.kind === 'note') {
            this.setTool('select');
            this.openTextEditor(shape.id);
          }
        }
        this.engine.markDirty();
        break;
      }
      case 'eraser': {
        if (drag.erased.length) {
          const ids = [...drag.erased];
          this._sendRaw(ids.map((id) => this.crdt.deleteShape(id)));
          this.crdt.undoStack.push({
            undoOps: ids.map((id) => ({ type: 'restore_shape', id })),
            redoOps: ids.map((id) => ({ type: 'delete_shape', id })),
            label: '擦除',
          });
          this.crdt.redoStack.length = 0;
          for (const id of ids) this.engine.selection.delete(id);
          this.engine.markDirty();
        }
        break;
      }
      default: break;
    }
  }

  /** 发送一批已带信封的操作(合并到本地 + 交给 onOps 发送), 不产生撤销记录 */
  _sendRaw(ops) {
    const list = ops.filter(Boolean);
    if (!list.length) return;
    mergeOps(this.shapes, list);
    if (this.crdt.onOps) {
      if (list.length === 1) this.crdt.onOps(list);
      else this.crdt.onOps([{ ...this.crdt._next(), type: 'batch', ts: Date.now(), base_rev: 0, ops: list }]);
    }
  }

  /* ------------------------------------------------------------ 滚轮缩放/平移 */
  _bindWheel() {
    this.engine.topCanvas.addEventListener('wheel', (e) => {
      e.preventDefault();
      const { sx, sy } = this._pos(e);
      if (e.ctrlKey || e.metaKey) {
        this.engine.zoomAt(Math.exp(-e.deltaY * 0.01), sx, sy);
      } else if (e.shiftKey) {
        this.engine.panBy(-e.deltaY, 0);
      } else {
        this.engine.panBy(-e.deltaX, -e.deltaY);
      }
      if (this.opts.onZoomChange) this.opts.onZoomChange(this.engine.cam.k);
    }, { passive: false });
  }

  /* ------------------------------------------------------------ 键盘 */
  _bindKeyboard() {
    window.addEventListener('keydown', (e) => {
      if (e.code === 'Space' && !this._isTyping(e)) {
        if (!this._spaceDown) {
          this._spaceDown = true;
          this.engine.topCanvas.style.cursor = 'grab';
        }
        e.preventDefault();
        return;
      }
      if (this._isTyping(e)) return;
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key.toLowerCase() === 'z' && !e.shiftKey) { this.undo(); e.preventDefault(); return; }
      if ((mod && e.key.toLowerCase() === 'y') || (mod && e.shiftKey && e.key.toLowerCase() === 'z')) { this.redo(); e.preventDefault(); return; }
      if (mod && e.key.toLowerCase() === 'c') { this.copy(); e.preventDefault(); return; }
      if (mod && e.key.toLowerCase() === 'v') { this.paste(); e.preventDefault(); return; }
      if (mod && e.key.toLowerCase() === 'd') { this.duplicate(); e.preventDefault(); return; }
      if (mod && e.key.toLowerCase() === 'a') { this.selectAll(); e.preventDefault(); return; }
      if (mod && (e.key === ']' || e.key === '[')) { this.bring(e.key === ']' ? 'front' : 'back'); e.preventDefault(); return; }
      if (!mod && TOOL_KEYS[e.key.toLowerCase()] && !e.altKey) {
        if (!this.readOnly || TOOL_KEYS[e.key.toLowerCase()] === 'pan') this.setTool(TOOL_KEYS[e.key.toLowerCase()]);
        return;
      }
      if (!this.readOnly && this.engine.selection.size) {
        if (e.key === 'Delete' || e.key === 'Backspace') { this.deleteSelection(); e.preventDefault(); return; }
        if (e.key.startsWith('Arrow')) {
          const step = e.shiftKey ? 10 : 1;
          const dx = e.key === 'ArrowLeft' ? -step : e.key === 'ArrowRight' ? step : 0;
          const dy = e.key === 'ArrowUp' ? -step : e.key === 'ArrowDown' ? step : 0;
          this.nudge(dx, dy);
          e.preventDefault();
        }
      }
      if (e.key === 'Escape') {
        this.closeTextEditor(false);
        this._edgeFirst = null;
        this.engine.draftEdge = null;
        this.engine.selection = new Set();
        this._notifySelection();
        this.engine.markDirty();
      }
    });
    window.addEventListener('keyup', (e) => {
      if (e.code === 'Space') {
        this._spaceDown = false;
        this.engine.topCanvas.style.cursor = this.cursorFor(this.tool);
      }
    });
  }

  _isTyping(e) {
    const t = e.target;
    return t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
  }

  /* ------------------------------------------------------------ 编辑动作 */
  undo() {
    if (this.readOnly) return;
    const entry = this.crdt.undo(this.shapes);
    if (entry) {
      for (const id of this.engine.selection) if (!this.shapes.get(id) || this.shapes.get(id).deleted) this.engine.selection.delete(id);
      this.engine.rebuildIndex();
      this.engine.markDirty();
      if (this.opts.onHistoryChange) this.opts.onHistoryChange();
    }
  }

  redo() {
    if (this.readOnly) return;
    const entry = this.crdt.redo(this.shapes);
    if (entry) {
      this.engine.rebuildIndex();
      this.engine.markDirty();
      if (this.opts.onHistoryChange) this.opts.onHistoryChange();
    }
  }

  deleteSelection() {
    if (this.readOnly || !this.engine.selection.size) return;
    const ids = [...this.engine.selection];
    this.crdt.commit(this.shapes, ids.map((id) => this.crdt.deleteShape(id)), { label: '删除' });
    for (const id of ids) {
      const shape = this.shapes.get(id);
      if (shape) this.engine.gridIndex.remove(shape);
    }
    this.engine.selection = new Set();
    this._notifySelection();
    this.engine.markDirty();
    if (this.opts.onHistoryChange) this.opts.onHistoryChange();
  }

  duplicate(clipboardSource = null) {
    if (this.readOnly) return;
    const source = clipboardSource || [...this.engine.selection]
      .map((id) => this.shapes.get(id)).filter((s) => s && !s.deleted);
    if (!source.length) return;
    const idMap = new Map();
    const clones = source.map((s) => {
      const copy = { ...stripPrivate(s), id: uidShape(), z: nextZ() };
      copy.points = s.points ? s.points.map((p) => [...p]) : undefined;
      if (copy.points === undefined) delete copy.points;
      idMap.set(s.id, copy.id);
      return copy;
    });
    // 内部引用(连线/父子)重映射
    for (const copy of clones) {
      for (const field of ['from', 'to', 'parent']) {
        if (copy[field] && idMap.has(copy[field])) copy[field] = idMap.get(copy[field]);
      }
    }
    const newShapes = clones.map((c) => createShape(c.kind, { ...c, x: (c.x || 0) + 24, y: (c.y || 0) + 24 }));
    for (const shape of newShapes) this.shapes.set(shape.id, shape);
    this.crdt.commit(this.shapes, newShapes.map((s) => this.crdt.addShape(stripPrivate(s))), { label: '副本' });
    for (const shape of newShapes) this.engine.gridIndex.insert(shape);
    this.engine.selection = new Set(newShapes.map((s) => s.id));
    this._notifySelection();
    this.engine.markDirty();
  }

  copy() {
    const source = [...this.engine.selection].map((id) => this.shapes.get(id))
      .filter((s) => s && !s.deleted);
    if (source.length) this._clipboard = source.map((s) => stripPrivate(s));
  }

  paste() {
    if (this.readOnly || !this._clipboard?.length) return;
    this.duplicate(this._clipboard.map((s) => ({ ...s })));
  }

  selectAll() {
    if (this.readOnly) return;
    this.engine.selection = new Set(
      [...this.shapes.values()].filter((s) => !s.deleted && s.kind !== 'ghost').map((s) => s.id),
    );
    this._notifySelection();
    this.engine.markDirty('overlay');
  }

  nudge(dx, dy) {
    if (this.readOnly) return;
    const ids = [...this.engine.selection];
    const ops = ids.map((id) => this.crdt.move(id, dx, dy));
    this.crdt.commit(this.shapes, ops, { label: '微移' });
    for (const id of ids) {
      const shape = this.shapes.get(id);
      if (shape) this.engine.gridIndex.refresh(shape);
    }
    this.engine.markDirty();
  }

  bring(where) {
    if (this.readOnly) return;
    syncZCounter(this.shapes);
    const ops = [];
    const ids = [...this.engine.selection];
    ids.forEach((id, i) => {
      const z = where === 'front' ? nextZ() : 0.5 - i * 0.001;
      ops.push(this.crdt.reorder(id, z));
    });
    if (ops.length) {
      this.crdt.commit(this.shapes, ops, { label: where === 'front' ? '置顶' : '置底' });
      this.engine.markDirty('main');
    }
  }

  /* ------------------------------------------------------------ 文本编辑 */
  openTextEditor(shapeId) {
    if (this.readOnly) return;
    this.closeTextEditor(false);
    const shape = this.shapes.get(shapeId);
    if (!shape || shape.deleted) return;
    this.engine.editingId = shapeId;
    const b = bboxOf(shape);
    const s0 = this.engine.worldToScreen(b.x0, b.y0);
    const s1 = this.engine.worldToScreen(b.x1, b.y1);
    const ta = document.createElement('textarea');
    ta.className = 'wb-inline-editor';
    ta.value = shape.text || '';
    const fs = Math.max(10, (Number(shape.fontSize) || 14) * this.engine.cam.k);
    ta.style.cssText = `
      position:absolute;left:${s0.x}px;top:${s0.y}px;
      width:${Math.max(60, s1.x - s0.x)}px;height:${Math.max(30, s1.y - s0.y)}px;
      font:${fs}px/${1.35} "PingFang SC","Microsoft YaHei",sans-serif;
      color:${shape.textColor || (shape.kind === 'note' ? '#3a3a35' : '#1f2937')};
      background:${shape.fill && shape.fill !== 'transparent' ? shape.fill : 'transparent'};
      border:2px solid ${this.engine.opts.accent};border-radius:6px;outline:none;
      resize:none;padding:6px 8px;text-align:center;z-index:50;overflow:hidden;
      box-shadow:0 4px 16px rgba(0,0,0,.18);`;
    if (['rect', 'ellipse', 'diamond', 'note', 'mindnode'].includes(shape.kind)) {
      ta.style.display = 'flex';
      ta.style.alignItems = 'center';
    }
    this.engine.container.appendChild(ta);
    ta.focus();
    ta.select();
    this._editor = { ta, shapeId };
    ta.addEventListener('keydown', (e) => {
      e.stopPropagation();
      if (e.key === 'Escape') { e.preventDefault(); this.closeTextEditor(false); return; }
      const multilineKind = shape.kind === 'note' || shape.kind === 'text';
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey || !multilineKind)) {
        e.preventDefault();
        this.closeTextEditor(true);
      }
    });
    ta.addEventListener('blur', () => this.closeTextEditor(true));
    this.engine.markDirty('main');
  }

  closeTextEditor(commit = true) {
    if (!this._editor) return;
    const { ta, shapeId } = this._editor;
    this._editor = null;
    const shape = this.shapes.get(shapeId);
    ta.remove();
    this.engine.editingId = null;
    if (commit && shape && !shape.deleted) {
      const text = ta.value;
      if (text !== (shape.text || '')) {
        if (!text.trim() && ['text', 'note'].includes(shape.kind) && !shape._keepEmpty) {
          // 空文本 → 删除该图形
          this.crdt.commit(this.shapes, [this.crdt.deleteShape(shapeId)], { label: '删除空文本' });
          this.engine.gridIndex.removeById(shapeId);
          this.engine.selection.delete(shapeId);
        } else {
          this.crdt.commit(this.shapes, [this.crdt.setProps(shapeId, { text })], { label: '编辑文本' });
        }
        if (this.opts.onHistoryChange) this.opts.onHistoryChange();
      }
    }
    this.engine.markDirty();
  }

  _onDblClick(e) {
    if (this.readOnly) return;
    const { sx, sy } = this._pos(e);
    const w = this.engine.screenToWorld(sx, sy);
    const shape = this.engine.hit(w.x, w.y);
    if (shape && shape.kind !== 'path' && shape.kind !== 'edge') {
      this.engine.selection = new Set([shape.id]);
      this._notifySelection();
      this.openTextEditor(shape.id);
      e.preventDefault();
      return;
    }
    if (!shape && this.tool === 'select') {
      // 双击空白 → 快速便签
      const note = createShape('note', {
        x: w.x - 95, y: w.y - 60, w: 190, h: 120, id: uidShape(),
        author: this.opts.me?.username || '',
      });
      this.shapes.set(note.id, note);
      this.engine.gridIndex.insert(note);
      this.crdt.commit(this.shapes, [this.crdt.addShape(stripPrivate(note))], { label: '便签' });
      this.engine.selection = new Set([note.id]);
      this._notifySelection();
      this.openTextEditor(note.id);
      this.engine.markDirty();
    }
  }

  /* ------------------------------------------------------------ 右键菜单 */
  _onContextMenu(e) {
    e.preventDefault();
    if (this.readOnly) return;
    const { sx, sy } = this._pos(e);
    const w = this.engine.screenToWorld(sx, sy);
    const shape = this.engine.hit(w.x, w.y);
    if (shape && !this.engine.selection.has(shape.id)) {
      this.engine.selection = new Set([shape.id]);
      this._notifySelection();
      this.engine.markDirty('overlay');
    }
    const hasSel = this.engine.selection.size > 0;
    this.showContextMenu(e.clientX, e.clientY, [
      { label: '✏️ 编辑文本', disabled: !hasSel, onClick: () => this.openTextEditor([...this.engine.selection][0]) },
      { label: '📋 复制', disabled: !hasSel, onClick: () => this.copy() },
      { label: '📄 粘贴', disabled: !this._clipboard, onClick: () => this.paste() },
      { label: '🧬 创建副本', disabled: !hasSel, onClick: () => this.duplicate() },
      { divider: true },
      { label: '⬆️ 置于顶层', disabled: !hasSel, onClick: () => this.bring('front') },
      { label: '⬇️ 置于底层', disabled: !hasSel, onClick: () => this.bring('back') },
      { divider: true },
      { label: '🗑 删除', danger: true, disabled: !hasSel, onClick: () => this.deleteSelection() },
    ]);
  }

  showContextMenu(clientX, clientY, items) {
    document.querySelector('.wb-ctx-menu')?.remove();
    const menu = document.createElement('div');
    menu.className = 'wb-ctx-menu';
    menu.style.cssText = `position:fixed;z-index:1200;left:${clientX}px;top:${clientY}px;min-width:160px;
      background:var(--bg-panel);border:1px solid var(--border);border-radius:8px;padding:5px;
      box-shadow:var(--shadow);font-size:13px`;
    for (const item of items) {
      if (item.divider) {
        const d = document.createElement('div');
        d.style.cssText = 'height:1px;background:var(--border-soft);margin:4px 2px';
        menu.appendChild(d);
        continue;
      }
      const el = document.createElement('div');
      el.textContent = item.label;
      el.style.cssText = `padding:6px 10px;border-radius:5px;cursor:${item.disabled ? 'default' : 'pointer'};
        color:${item.disabled ? 'var(--text-mute)' : item.danger ? 'var(--red)' : 'var(--text)'}`;
      if (!item.disabled) {
        el.onmouseenter = () => { el.style.background = 'var(--bg-hover)'; };
        el.onmouseleave = () => { el.style.background = ''; };
        el.onclick = () => { menu.remove(); item.onClick(); };
      }
      menu.appendChild(el);
    }
    document.body.appendChild(menu);
    const rect = menu.getBoundingClientRect();
    if (rect.right > window.innerWidth) menu.style.left = `${window.innerWidth - rect.width - 8}px`;
    if (rect.bottom > window.innerHeight) menu.style.top = `${window.innerHeight - rect.height - 8}px`;
    setTimeout(() => {
      const away = (ev) => { if (!menu.contains(ev.target)) { menu.remove(); document.removeEventListener('pointerdown', away); } };
      document.addEventListener('pointerdown', away);
    }, 10);
  }

  /* ------------------------------------------------------------ presence */
  _throttledPresence(w) {
    const now = performance.now();
    if (now - this._presenceThrottle < 80) return;
    this._presenceThrottle = now;
    this._emitPresence(w);
  }

  _emitPresence(w = null) {
    if (!this.opts.onPresence) return;
    this.opts.onPresence({
      cursor: w ? { x: Math.round(w.x), y: Math.round(w.y) } : undefined,
      tool: this.tool,
      selection: [...this.engine.selection],
      page: this.engine.page,
    });
  }

  _notifySelection() {
    if (this.opts.onSelectionChange) this.opts.onSelectionChange([...this.engine.selection]);
    this._emitPresence();
  }
}

export { stripPrivate };
