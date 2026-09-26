/* ================================================================
   crdt-client.js —— 客户端 CRDT 镜像 + 操作工厂 + 撤销/重做

   与 backend/crdt.py 语义一一对应:
   - move 是增量(delta): 并发移动同一图形时两端增量相加, 收敛一致;
   - set_props / reorder / reparent / delete / restore 是字段级
     LWW 寄存器, 时钟 = (lamport, site), 平票按 site 字典序;
   - op_id 全局唯一, 服务端幂等去重, 断线补发安全。

   撤销 = 发布语义逆操作(而非回滚), 因此他人并发编辑不受影响;
   重做 = 用新时钟重新发布原变更。
   ================================================================ */

export const LWW_FIELDS = new Set([
  'fill', 'stroke', 'strokeWidth', 'fontSize', 'fontFamily', 'fontWeight',
  'fontStyle', 'opacity', 'dash', 'text', 'textColor', 'cornerRadius',
  'collapsed', 'from', 'to', 'waypoints', 'w', 'h', 'x', 'y', 'name',
  'tag', 'strokeStyle', 'arrowStart', 'arrowEnd',
]);

export function clockOf(op) { return [Number(op.lam) || 0, String(op.site || '')]; }

/** 量化到 6 位小数(与后端 _finite_number 一致): 保证增量求和顺序无关 */
export function q6(v) { return Math.round(Number(v) * 1e6) / 1e6; }

const NUMERIC_SHAPE_FIELDS = ['x', 'y', 'w', 'h', 'z', 'rotation', 'strokeWidth',
  'fontSize', 'opacity', 'cornerRadius'];

/** 发出 add_shape 前量化图形数值(与服务端 sanitize 后的广播值一致) */
export function quantizeShape(shape) {
  const out = { ...shape };
  for (const f of NUMERIC_SHAPE_FIELDS) {
    if (out[f] != null) out[f] = q6(out[f]);
  }
  if (Array.isArray(out.points)) out.points = out.points.map((p) => [q6(p[0]), q6(p[1])]);
  return out;
}

/** 未知 id 的延迟物化占位(与后端 _placeholder 一致) */
function placeholder(shapes, id) {
  const ph = { id, kind: 'ghost', deleted: true, fc: {}, x: 0, y: 0, z: 1 };
  shapes.set(id, ph);
  return ph;
}

/** a 时钟是否严格大于 b([lam, site] 或 null) */
export function clockGt(a, b) {
  if (!a) return false;
  if (!b) return true;
  if (a[0] !== b[0]) return a[0] > b[0];
  return String(a[1]) > String(b[1]);
}

function fcGet(shape, field) {
  const entry = shape.fc?.[field];
  return Array.isArray(entry) && entry.length === 2 ? [entry[0], String(entry[1])] : null;
}

function fcSet(shape, field, clock) {
  (shape.fc = shape.fc || {})[field] = [clock[0], clock[1]];
}

function lwwPut(shape, field, value, clock) {
  if (!clockGt(clock, fcGet(shape, field))) return false;
  shape[field] = value;
  fcSet(shape, field, clock);
  return true;
}

/**
 * 把一个(远端或本地回声)操作合并进 shapes Map。幂等 + 乱序安全。
 * @param {Map<string,object>} shapes id → shape
 * @param {object} op 已带信封的操作(batch 自动展开)
 * @returns {Set<string>|null} 受影响图形 id 集合; null=无变化
 */
export function mergeOp(shapes, op) {
  if (!op || !op.type) return null;
  if (op.type === 'batch') {
    const touched = new Set();
    for (const sub of op.ops || []) {
      const subTouched = mergeOp(shapes, sub);
      if (subTouched) subTouched.forEach((id) => touched.add(id));
    }
    return touched.size ? touched : null;
  }
  const clock = clockOf(op);
  switch (op.type) {
    case 'add_shape': {
      const incoming = op.shape;
      if (!incoming?.id) return null;
      const existing = shapes.get(incoming.id);
      if (!existing) {
        const shape = { ...incoming, deleted: false, fc: { ...(incoming.fc || {}) } };
        for (const key of Object.keys(shape)) {
          if (['id', 'fc', 'meta', 'deleted'].includes(key)) continue;
          fcSet(shape, key, clock);
        }
        fcSet(shape, 'deleted', clock);
        shapes.set(shape.id, shape);
        return new Set([shape.id]);
      }
      let changed = false;
      // kind/meta 走 LWW: 墓碑(无 kind 时钟)被最大时钟的 add 吸收, 与顺序无关
      if (incoming.kind && clockGt(clock, fcGet(existing, 'kind'))) {
        existing.kind = incoming.kind;
        fcSet(existing, 'kind', clock);
        if (incoming.meta) existing.meta = incoming.meta;
        changed = true;
      }
      for (const [key, value] of Object.entries(incoming)) {
        if (['id', 'fc', 'meta', 'kind'].includes(key)) continue;
        if (key === 'x' || key === 'y') {
          // 位置基座注入(与后端一致): 未物化时把 add 的基座坐标加到
          // 已累计的 move 增量上; 已物化则跳过(re-add 不重置位置)
          if (!fcGet(existing, key)) {
            existing[key] = q6((Number(existing[key]) || 0) + (Number(value) || 0));
            fcSet(existing, key, clock);
            changed = true;
          }
          continue;
        }
        if (lwwPut(existing, key, value, clock)) changed = true;
      }
      // 复活判定统一走 deleted 字段 LWW(add 携带 deleted=False)
      if (lwwPut(existing, 'deleted', false, clock)) changed = true;
      return changed ? new Set([existing.id]) : null;
    }
    case 'move': {
      // 延迟物化 + 对已删除图形同样累计(交换律)
      const shape = shapes.get(op.id) || placeholder(shapes, op.id);
      shape.x = q6((Number(shape.x) || 0) + (Number(op.dx) || 0));
      shape.y = q6((Number(shape.y) || 0) + (Number(op.dy) || 0));
      return new Set([op.id]);
    }
    case 'path_extend': {
      const shape = shapes.get(op.id) || placeholder(shapes, op.id);
      (shape.points = shape.points || []).push(...(op.points || []));
      return new Set([op.id]);
    }
    case 'truncate_path': {
      const shape = shapes.get(op.id) || placeholder(shapes, op.id);
      const pts = shape.points || [];
      const len = Math.max(0, Number(op.length) || 0);
      if (pts.length <= len) return null;
      shape.points = pts.slice(0, len);
      return new Set([op.id]);
    }
    case 'delete_shape':
    case 'restore_shape': {
      const wantDelete = op.type === 'delete_shape';
      const shape = shapes.get(op.id) || placeholder(shapes, op.id);
      if (lwwPut(shape, 'deleted', wantDelete, clock)) return new Set([op.id]);
      return null;
    }
    case 'reorder': {
      const shape = shapes.get(op.id) || placeholder(shapes, op.id);
      if (lwwPut(shape, 'z', Number(op.z) || 0, clock)) return new Set([op.id]);
      return null;
    }
    case 'reparent': {
      const shape = shapes.get(op.id) || placeholder(shapes, op.id);
      const parent = op.parent || null;
      if (parent === op.id) return null;
      if (parent && createsCycle(shapes, op.id, parent)) return null;
      if (lwwPut(shape, 'parent', parent, clock)) return new Set([op.id]);
      return null;
    }
    case 'set_props': {
      const shape = shapes.get(op.id) || placeholder(shapes, op.id);
      let changed = false;
      for (const [field, value] of Object.entries(op.props || {})) {
        if (field === 'parent') continue;
        if (field === 'z' && value != null) {
          if (lwwPut(shape, 'z', Number(value) || 0, clock)) changed = true;
          continue;
        }
        if (lwwPut(shape, field, value, clock)) changed = true;
      }
      return changed ? new Set([op.id]) : null;
    }
    default:
      return null;
  }
}

export function createsCycle(shapes, nodeId, newParent) {
  let cursor = newParent;
  let hops = 0;
  while (cursor && hops < 1000) {
    if (cursor === nodeId) return true;
    cursor = shapes.get(cursor)?.parent || null;
    hops += 1;
  }
  return false;
}

/** 批量合并, 返回受影响 id 集合 */
export function mergeOps(shapes, ops) {
  const touched = new Set();
  for (const op of ops || []) {
    const ids = mergeOp(shapes, op);
    if (ids) ids.forEach((id) => touched.add(id));
  }
  return touched;
}

/** 从快照/状态接口载入全量图形 */
export function loadShapes(shapesArray) {
  const map = new Map();
  for (const shape of shapesArray || []) {
    if (shape && shape.id) map.set(shape.id, { ...shape });
  }
  return map;
}

/* ================================================================
   CrdtClient —— 操作工厂(lamport 时钟) + 撤销/重做栈
   ================================================================ */
export class CrdtClient {
  /** @param {string} siteId 站点 ID(与 WS clientId 一致) */
  constructor(siteId) {
    this.site = siteId;
    this.lam = 0;
    this.seq = 0;
    this.undoStack = [];        // [{ops, inverse, label}]
    this.redoStack = [];
    this.maxDepth = 100;
    this.onOps = null;          // (ops[]) => void  由页面注入(发送到 WS)
  }

  /** 观察到远端操作时推进 lamport 时钟 */
  witness(op) {
    const lam = Number(op?.lam) || 0;
    if (lam > this.lam) this.lam = lam;
    if (op?.type === 'batch') (op.ops || []).forEach((sub) => this.witness(sub));
  }

  _next() {
    this.lam += 1;
    this.seq += 1;
    return { site: this.site, lam: this.lam, op_id: `${this.site}:${this.seq}` };
  }

  _envelope(type, payload, baseRev = 0) {
    const head = this._next();
    return { ...head, type, ts: Date.now(), base_rev: baseRev, ...payload };
  }

  /* ------------------------------------------------------------ 操作构造
     所有数值在构造时量化到 6 位小数 —— 与服务端 sanitize 后的广播值
     一致, 保证「发送方乐观状态」与「其他副本状态」逐位相同。 */
  addShape(shape, baseRev = 0) {
    return this._envelope('add_shape', { shape: quantizeShape(shape) }, baseRev);
  }

  move(id, dx, dy, baseRev = 0) {
    return this._envelope('move', { id, dx: q6(dx), dy: q6(dy) }, baseRev);
  }

  setProps(id, props, baseRev = 0) {
    const clean = {};
    for (const [k, v] of Object.entries(props || {})) {
      clean[k] = (v != null && NUMERIC_SHAPE_FIELDS.includes(k)) ? q6(v) : v;
    }
    return this._envelope('set_props', { id, props: clean }, baseRev);
  }

  deleteShape(id, baseRev = 0) {
    return this._envelope('delete_shape', { id }, baseRev);
  }

  restoreShape(id, baseRev = 0) {
    return this._envelope('restore_shape', { id }, baseRev);
  }

  reorder(id, z, baseRev = 0) {
    return this._envelope('reorder', { id, z: q6(z) }, baseRev);
  }

  reparent(id, parent, baseRev = 0) {
    return this._envelope('reparent', { id, parent: parent || null }, baseRev);
  }

  pathExtend(id, points, baseRev = 0) {
    return this._envelope('path_extend',
      { id, points: (points || []).map((p) => [q6(p[0]), q6(p[1])]) }, baseRev);
  }

  truncatePath(id, length, baseRev = 0) {
    return this._envelope('truncate_path', { id, length }, baseRev);
  }

  batch(ops, baseRev = 0) {
    if (!ops?.length) return null;
    if (ops.length === 1) { ops[0].base_rev = baseRev; return ops[0]; }
    return this._envelope('batch', { ops }, baseRev);
  }

  /* ------------------------------------------------------------ 提交与撤销 */
  /**
   * 提交一组本地操作: 乐观应用到 shapes, 记录撤销信息, 发送出去。
   * 撤销栈保存的是「语义操作」(不带信封); 撤销/重做执行时用 reissue
   * 重新签发新 op_id/lamport —— 旧 op_id 会被服务端幂等去重, 必须换新。
   * @param {Map} shapes
   * @param {object[]} ops 叶子操作数组(commit 内部负责包 batch)
   */
  commit(shapes, ops, { label = '' } = {}) {
    const list = (Array.isArray(ops) ? ops : [ops]).filter(Boolean);
    if (!list.length) return null;
    const captured = snapshotAffected(shapes, list);
    mergeOps(shapes, list);
    const inverse = invertOps(list, captured);           // 语义逆操作
    const recipe = list.map(cloneOp);                    // 语义原操作(重做用)
    this.undoStack.push({ undoOps: inverse, redoOps: recipe, label });
    if (this.undoStack.length > this.maxDepth) this.undoStack.shift();
    this.redoStack.length = 0;
    this._emit(list);
    return list;
  }

  /** 只发送不记撤销(如实时拖动的中间增量帧) */
  send(shapes, ops) {
    const list = (Array.isArray(ops) ? ops : [ops]).filter(Boolean);
    if (!list.length) return;
    mergeOps(shapes, list);
    this._emit(list);
  }

  _emit(list) {
    if (!this.onOps) return;
    if (list.length === 1) this.onOps(list);
    else this.onOps([{ ...this._next(), type: 'batch', ts: Date.now(), base_rev: 0, ops: list }]);
  }

  canUndo() { return this.undoStack.length > 0; }
  canRedo() { return this.redoStack.length > 0; }

  /** 撤销: 发布语义逆操作(新时钟新 op_id), 他人并发编辑不受影响 */
  undo(shapes) {
    const entry = this.undoStack.pop();
    if (!entry) return null;
    const fresh = reissue(entry.undoOps, this);
    mergeOps(shapes, fresh);
    this.redoStack.push({ redoOps: entry.redoOps, undoOps: entry.undoOps, label: entry.label });
    if (this.redoStack.length > this.maxDepth) this.redoStack.shift();
    this._emit(fresh);
    return entry;
  }

  /** 重做: 用新时钟重新发布原变更 */
  redo(shapes) {
    const entry = this.redoStack.pop();
    if (!entry) return null;
    const fresh = reissue(entry.redoOps, this);
    mergeOps(shapes, fresh);
    this.undoStack.push({ undoOps: entry.undoOps, redoOps: entry.redoOps, label: entry.label });
    this._emit(fresh);
    return entry;
  }

  clearHistory() {
    this.undoStack.length = 0;
    this.redoStack.length = 0;
  }
}

function cloneOp(op) { return JSON.parse(JSON.stringify(op)); }

/** 用当前时钟重新签发一批操作(新 op_id/lam/ts, 语义不变) */
function reissue(ops, client) {
  return (ops || []).map((op) => {
    if (op.type === 'batch') {
      return { ...client._next(), type: 'batch', ts: Date.now(), ops: reissue(op.ops, client) };
    }
    const { op_id, lam, ts, rev, by, dup, base_rev, ...payload } = op;
    return { ...client._next(), ts: Date.now(), base_rev: 0, ...payload };
  });
}

/** 收集操作涉及的图形当前浅拷贝(生成逆操作用) */
export function snapshotAffected(shapes, ops) {
  const out = new Map();
  const collect = (op) => {
    if (op.type === 'batch') { (op.ops || []).forEach(collect); return; }
    const id = op.id || op.shape?.id;
    if (id && shapes.has(id) && !out.has(id)) out.set(id, cloneOp(shapes.get(id)));
  };
  (ops || []).forEach(collect);
  return out;
}

/** 构造逆操作序列(与 backend/crdt.invert_ops 语义一致) */
export function invertOps(ops, beforeMap) {
  const out = [];
  for (const op of [...(ops || [])].reverse()) {
    if (op.type === 'batch') { out.push(...invertOps(op.ops, beforeMap)); continue; }
    switch (op.type) {
      case 'add_shape':
        if (op.shape?.id) out.push({ type: 'delete_shape', id: op.shape.id });
        break;
      case 'delete_shape':
        out.push({ type: 'restore_shape', id: op.id });
        break;
      case 'restore_shape':
        out.push({ type: 'delete_shape', id: op.id });
        break;
      case 'move':
        out.push({ type: 'move', id: op.id, dx: -(op.dx || 0), dy: -(op.dy || 0) });
        break;
      case 'set_props': {
        const before = beforeMap.get(op.id) || {};
        const props = {};
        for (const field of Object.keys(op.props || {})) props[field] = before[field] ?? null;
        out.push({ type: 'set_props', id: op.id, props });
        break;
      }
      case 'reorder': {
        const before = beforeMap.get(op.id) || {};
        out.push({ type: 'reorder', id: op.id, z: before.z ?? 1 });
        break;
      }
      case 'reparent': {
        const before = beforeMap.get(op.id) || {};
        out.push({ type: 'reparent', id: op.id, parent: before.parent ?? null });
        break;
      }
      case 'path_extend': {
        const before = beforeMap.get(op.id) || {};
        out.push({ type: 'truncate_path', id: op.id, length: (before.points || []).length });
        break;
      }
      case 'truncate_path': {
        const before = beforeMap.get(op.id) || {};
        const full = (before.points || []);
        out.push({ type: 'path_extend', id: op.id, points: full.slice(op.length || 0) });
        break;
      }
      default: break;
    }
  }
  return out;
}
