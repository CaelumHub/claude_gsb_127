/* ================================================================
   replay.js —— 历史回放播放器

   **难点: 历史压缩与快速回放**
   - 初始化: GET /history/replay → 「最近快照 + 其后操作页」, 不用从
     rev 0 重放;
   - 播放: rAF 循环按 速度×基准速率 逐个折叠操作(mergeOp 与协同
     实时路径共用同一套 CRDT 合并语义, 所见即当时真实状态);
   - 快进(≥4x)与拖动进度条: 客户端把同站点同图形时间窗内的连续
     move 增量合并为一步(coalesceMoves, 与服务端归档压缩同规则),
     长拖拽瞬间演完;
   - 拖动进度条任意跳转: 向服务端请求 ≤目标 rev 的最近快照, 服务端
     折叠好交回来, 前端一次导入 —— 快照间隔 200 时跳转近乎瞬时;
   - 流式: 操作按页(500 条)拉取, 播放到页尾自动续拉。
   ================================================================ */
import { Api } from './api.js';
import { mergeOp, q6 } from './crdt-client.js';

const BASE_OPS_PER_SEC = 14;          // 1x 速度下每秒折叠的操作数
const PAGE_SIZE = 500;
const COALESCE_WINDOW_MS = 900;

export const OP_TYPE_META = {
  add_shape: { color: '#4fc08d', label: '新增' },
  delete_shape: { color: '#e8697d', label: '删除' },
  restore_shape: { color: '#f0a35e', label: '恢复' },
  move: { color: '#5b8ff9', label: '移动' },
  set_props: { color: '#a88ce8', label: '属性' },
  reorder: { color: '#e8c56a', label: '层级' },
  reparent: { color: '#6dc8ec', label: '挂载' },
  path_extend: { color: '#61c0a8', label: '笔迹' },
  truncate_path: { color: '#d4709c', label: '截断' },
  batch: { color: '#8a93a5', label: '批量' },
};

/** 操作归属用户键: 优先用服务端盖章的 by, 缺失时退回站点 ID(与服务端一致) */
function opUserKey(op) {
  return String(op.by || op.site || '');
}

/** 收集操作(含 batch 子操作)直接寻址的图形 id(与服务端 _op_targets 一致) */
function opTargets(op) {
  if (op.type === 'batch') {
    return (op.ops || [])
      .map((sub) => sub.id || sub.shape?.id)
      .filter(Boolean)
      .map(String);
  }
  const sid = op.id || op.shape?.id;
  return sid ? [String(sid)] : [];
}

/**
 * 与服务端 coalesce_moves 同规则的客户端合并(快进用):
 * 仅合并「同一用户(by, 退回 site)、同一图形、相邻 move 均在时间窗内、
 * 且中间没有夹任何针对该图形的其他操作(含 set_props 改色/改字)」的连续 move。
 */
export function coalesceMoves(ops, windowMs = COALESCE_WINDOW_MS) {
  const out = [];
  // 嵌套 Map: userKey → (shapeId → op), 精确按「用户+图形」两段寻址,
  // 不能用 `${user}|${id}` 配 endsWith(id)(图形 id 互为后缀时会误冲断)
  const pending = new Map();
  const bucket = (userKey) => {
    let m = pending.get(userKey);
    if (!m) { m = new Map(); pending.set(userKey, m); }
    return m;
  };
  const flushKeyOf = (m, id) => { const agg = m.get(id); if (agg) { out.push(agg); m.delete(id); } };
  const flushShape = (id) => {
    for (const m of pending.values()) if (m.has(id)) flushKeyOf(m, id);
  };
  for (const op of ops) {
    if (op.type === 'move') {
      const userKey = opUserKey(op);
      const m = bucket(userKey);
      const agg = m.get(op.id);
      // 滑动时间窗: 相对聚合段内上一条 move, 相邻间隔均需 ≤ 窗口
      if (agg && (op.ts || 0) - (agg.ts || 0) <= windowMs) {
        agg.dx = q6((agg.dx || 0) + (op.dx || 0));
        agg.dy = q6((agg.dy || 0) + (op.dy || 0));
        agg.ts = op.ts;
        agg.rev = op.rev;
        agg._merged = (agg._merged || 1) + 1;
        continue;
      }
      if (agg) out.push(agg);
      m.set(op.id, { ...op });
      continue;
    }
    // 任何(含 batch 子操作)寻址到该图形的中间操作都冲断该图形的合并链
    for (const id of opTargets(op)) flushShape(id);
    out.push(op);
  }
  for (const m of pending.values()) for (const agg of m.values()) out.push(agg);
  return out;
}

export class ReplayPlayer {
  /**
   * @param {object} deps { engine(BoardEngine readOnly), boardId,
   *   onRev(rev, op), onStateChange, onLoaded(meta), timeline(Canvas 可选) }
   */
  constructor({ engine, boardId, onRev = null, onLoaded = null, timeline = null }) {
    this.engine = engine;
    this.boardId = boardId;
    this.onRev = onRev;
    this.onLoaded = onLoaded;
    this.timeline = timeline;

    this.shapes = new Map();
    engine.setShapesMap(this.shapes);

    this.headRev = 0;
    this.currentRev = 0;
    this.ops = [];                  // 已加载的操作(升序)
    this.opsStartRev = 0;           // ops[0] 之前的 rev 基线
    this.snapshots = [];
    this.shards = [];
    this.stats = null;

    this.playing = false;
    this.speed = 2;
    this._acc = 0;
    this._lastFrame = 0;
    this._raf = null;
    this._fetching = false;
    this._timelineHover = null;

    if (timeline) this._bindTimeline();
    this._loop = this._loop.bind(this);
    this._raf = requestAnimationFrame(this._loop);
  }

  /* ------------------------------------------------------------ 加载 */
  async load(atRev = null) {
    const data = await Api.history.replay(this.boardId, {
      rev: atRev ?? undefined, coalesce: false, limit: PAGE_SIZE,
    });
    this.headRev = data.head_rev;
    this.shapes.clear();
    if (data.snapshot?.shapes) {
      for (const s of data.snapshot.shapes) this.shapes.set(s.id, { ...s });
      this.currentRev = data.base_rev || 0;
    } else {
      this.currentRev = 0;
    }
    this.ops = data.ops || [];
    this.opsStartRev = this.currentRev;
    this._applyOpsUpTo(atRev ?? this.headRev, true);

    const index = await Api.history.index(this.boardId);
    this.snapshots = index.snapshots || [];
    this.shards = index.shards || [];
    this.stats = index.stats || null;
    this.headRev = index.head_rev ?? this.headRev;
    this.engine.rebuildIndex();
    this.engine.fitToContent();
    this.drawTimeline();
    if (this.onLoaded) this.onLoaded(this);
    return this;
  }

  /** 跳转: 服务端找快照折叠, 前端一次导入(快速回放核心) */
  async seek(rev) {
    rev = Math.max(0, Math.min(this.headRev, Math.round(rev)));
    const data = await Api.history.replay(this.boardId, { rev, coalesce: true, limit: 5000 });
    this.shapes.clear();
    if (data.snapshot?.shapes) {
      for (const s of data.snapshot.shapes) this.shapes.set(s.id, { ...s });
      this.currentRev = data.base_rev || 0;
    } else this.currentRev = 0;
    this.ops = data.ops || [];
    this.opsStartRev = this.currentRev;
    this._applyOpsUpTo(rev, false);
    this.engine.rebuildIndex();
    this.engine.markDirty();
    this.drawTimeline();
    if (this.onRev) this.onRev(this.currentRev, null);
  }

  async _ensureOpsUntil(rev) {
    if (this._fetching) return;
    const lastLoaded = this.ops.length ? this.ops[this.ops.length - 1].rev : this.opsStartRev;
    if (lastLoaded >= rev) return;
    this._fetching = true;
    try {
      const data = await Api.history.ops(this.boardId, {
        from_rev: lastLoaded, limit: PAGE_SIZE,
      });
      if (data.ops?.length) this.ops.push(...data.ops);
    } finally {
      this._fetching = false;
    }
  }

  /** 把已加载操作折叠到目标 rev; silent=true 时不逐个回调 */
  _applyOpsUpTo(rev, silent) {
    let i = 0;
    while (i < this.ops.length) {
      const op = this.ops[i];
      if ((op.rev || 0) > rev) break;
      if ((op.rev || 0) <= this.currentRev) { i += 1; continue; }
      const touched = mergeOp(this.shapes, op);
      this.engine.onShapesChanged(touched);
      this.currentRev = op.rev;
      if (!silent && this.onRev) this.onRev(this.currentRev, op);
      i += 1;
    }
  }

  /* ------------------------------------------------------------ 播放控制 */
  play() {
    if (this.playing) return;
    if (this.currentRev >= this.headRev) this.seek(0).then(() => this._startPlay());
    else this._startPlay();
  }

  _startPlay() {
    this.playing = true;
    this._lastFrame = performance.now();
  }

  pause() { this.playing = false; }

  toggle() { this.playing ? this.pause() : this.play(); }

  setSpeed(speed) {
    this.speed = Math.max(0.5, Math.min(32, speed));
  }

  async step(delta = 1) {
    this.pause();
    if (delta > 0) {
      await this._ensureOpsUntil(this.currentRev + delta * 2 + 10);
      for (let n = 0; n < delta; n++) {
        const op = this._nextOpAfter(this.currentRev);
        if (!op) break;
        const touched = mergeOp(this.shapes, op);
        this.engine.onShapesChanged(touched);
        this.currentRev = op.rev;
        if (this.onRev) this.onRev(this.currentRev, op);
      }
    } else {
      // 后退: 跳到 rev+delta 的最近状态(通过服务端快照折叠)
      await this.seek(Math.max(0, this.currentRev + delta));
    }
    this.drawTimeline();
  }

  _nextOpAfter(rev) {
    for (const op of this.ops) if ((op.rev || 0) > rev) return op;
    return null;
  }

  _loop(now) {
    this._raf = requestAnimationFrame(this._loop);
    if (!this.playing) return;
    const dt = Math.min(0.25, (now - this._lastFrame) / 1000);
    this._lastFrame = now;
    const fast = this.speed >= 4;
    this._acc += dt * BASE_OPS_PER_SEC * this.speed;
    let budget = Math.floor(this._acc);
    if (budget <= 0) return;
    this._acc -= budget;

    let applied = 0;
    let lastOp = null;
    let touchedAll = new Set();
    while (budget > 0) {
      let op = this._nextOpAfter(this.currentRev);
      if (!op) {
        if (this.currentRev >= this.headRev) { this.pause(); this._emitEnd?.(); break; }
        this._ensureOpsUntil(this.currentRev + budget + 2);
        break;                                   // 等待下一页, 下帧继续
      }
      if (fast && op.type === 'move') {
        // 快进: 仅合并「同一用户、同一图形、相邻时间窗内、且中间没有夹
        // 任何针对该图形的其他操作(改色/改字等)」的连续 move
        const userKey = opUserKey(op);
        let dx = op.dx || 0; let dy = op.dy || 0;
        let j = this.ops.indexOf(op) + 1;
        let merged = 1;
        let lastTs = op.ts || 0;
        let lastRev = op.rev;
        while (j < this.ops.length && merged < budget) {
          const nxt = this.ops[j];
          if (nxt.type === 'move' && nxt.id === op.id && opUserKey(nxt) === userKey
            && (nxt.ts || 0) - lastTs <= COALESCE_WINDOW_MS) {
            dx = q6(dx + (nxt.dx || 0)); dy = q6(dy + (nxt.dy || 0));
            lastTs = nxt.ts || 0;
            lastRev = nxt.rev; merged += 1; j += 1;
          } else break;
        }
        const coalesced = { ...op, dx, dy, rev: lastRev, _merged: merged };
        const touched = mergeOp(this.shapes, coalesced);
        if (touched) touched.forEach((id) => touchedAll.add(id));
        this.currentRev = lastRev;
        budget -= merged;
        lastOp = coalesced;
        applied += merged;
        continue;
      }
      const touched = mergeOp(this.shapes, op);
      if (touched) touched.forEach((id) => touchedAll.add(id));
      this.currentRev = op.rev;
      lastOp = op;
      budget -= 1;
      applied += 1;
    }
    if (applied) {
      this.engine.onShapesChanged(touchedAll);
      if (this.onRev) this.onRev(this.currentRev, lastOp);
      this.drawTimeline();
    }
  }

  onEnd(fn) { this._emitEnd = fn; }

  destroy() {
    if (this._raf) cancelAnimationFrame(this._raf);
  }

  /* ------------------------------------------------------------ 时间轴 */
  _bindTimeline() {
    const canvas = this.timeline;
    const seekFromEvent = async (e) => {
      const rect = canvas.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      await this.seek(ratio * this.headRev);
    };
    let dragging = false;
    canvas.addEventListener('pointerdown', (e) => {
      dragging = true;
      this.pause();
      canvas.setPointerCapture(e.pointerId);
      seekFromEvent(e);
    });
    canvas.addEventListener('pointermove', (e) => {
      const rect = canvas.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      this._timelineHover = { ratio, rev: Math.round(ratio * this.headRev) };
      if (dragging) {
        this._scrubPending = this._scrubPending || Promise.resolve();
        this._scrubPending = this._scrubPending.then(() => seekFromEvent(e));
      }
      this.drawTimeline();
    });
    canvas.addEventListener('pointerup', () => { dragging = false; });
    canvas.addEventListener('pointerleave', () => { this._timelineHover = null; this.drawTimeline(); });
  }

  drawTimeline() {
    const canvas = this.timeline;
    if (!canvas) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const rect = canvas.getBoundingClientRect();
    if (rect.width < 10) return;
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = rect.width; const H = rect.height;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = 'var(--bg-elevated)';
    ctx.fillStyle = getComputedStyle(document.body).getPropertyValue('--bg-elevated') || '#252a37';
    ctx.fillRect(0, 0, W, H);

    const head = Math.max(1, this.headRev);
    const xOf = (rev) => (rev / head) * (W - 4) + 2;

    // 分片边界(淡竖线)
    ctx.strokeStyle = 'rgba(140,150,170,0.18)';
    ctx.lineWidth = 1;
    for (const shard of this.shards || []) {
      if (!shard.first_rev) continue;
      const x = xOf(shard.first_rev);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    }

    // 已加载操作按类型着色(密度条)
    const barH = H - 22;
    for (const op of this.ops) {
      const meta = OP_TYPE_META[op.type] || OP_TYPE_META.batch;
      ctx.fillStyle = meta.color;
      ctx.globalAlpha = 0.75;
      const x = xOf(op.rev || 0);
      const w = Math.max(1.2, W / head * 0.9);
      ctx.fillRect(x, 14, w, barH);
    }
    ctx.globalAlpha = 1;

    // 快照标记
    for (const snap of this.snapshots || []) {
      const x = xOf(snap.rev);
      ctx.fillStyle = '#4fc08d';
      ctx.beginPath();
      ctx.moveTo(x, 0); ctx.lineTo(x + 5, 7); ctx.lineTo(x - 5, 7);
      ctx.closePath(); ctx.fill();
    }

    // 播放头
    const px = xOf(this.currentRev);
    ctx.strokeStyle = '#ff6b6b';
    ctx.lineWidth = 1.6;
    ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, H); ctx.stroke();
    ctx.fillStyle = '#ff6b6b';
    ctx.beginPath(); ctx.arc(px, H - 5, 4, 0, Math.PI * 2); ctx.fill();

    // rev 刻度文字
    ctx.fillStyle = 'rgba(160,170,190,0.9)';
    ctx.font = '10px monospace';
    ctx.textBaseline = 'top';
    ctx.fillText('rev 0', 4, 2);
    ctx.textAlign = 'right';
    ctx.fillText(`rev ${this.headRev}`, W - 4, 2);
    ctx.textAlign = 'center';
    ctx.fillText(`▶ ${this.currentRev}`, Math.max(24, Math.min(W - 24, px)), 2);

    // 悬停提示
    if (this._timelineHover) {
      const { rev } = this._timelineHover;
      const label = `rev ${rev}`;
      const tw = ctx.measureText(label).width + 12;
      const hx = Math.max(2, Math.min(W - tw - 2, xOf(rev) - tw / 2));
      ctx.fillStyle = 'rgba(20,22,30,0.92)';
      ctx.beginPath(); ctx.roundRect(hx, H - 20, tw, 16, 4); ctx.fill();
      ctx.fillStyle = '#fff';
      ctx.fillText(label, hx + tw / 2, H - 18);
    }
  }
}
