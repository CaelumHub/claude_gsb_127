/* ================================================================
   mindmap.js —— 思维导图控制器

   - 层级模型: mindnode 图形的 parent 字段构成树(CRDT reparent 带环
     检测); collapsed 标志隐藏子树。
   - 自动布局: Reingold-Tilford 风格的整洁树算法 —— 叶子按次序占据
     纵向槽位, 内部节点取首末子节点中点, 层级横向排布; 支持
     right(向右)/both(左右分布)/down(向下组织架构图) 三种方向。
   - 协同策略: 「谁改动结构, 谁负责重排」—— 本地结构编辑(增删节点、
     折叠)触发本地重排并把结果以 move 增量广播; 远端的结构操作只
     刷新大纲, 位置以对方广播的 move 为准。两端的 move 是可交换
     增量, 即使同时重排也能收敛, 再按一次「整理布局」即可归位。
   ================================================================ */
import {
  bboxOf, createShape, mindNodeSize, nextZ, syncZCounter, uidShape,
} from './shapes.js';

const LEVEL_FILLS = ['#3b6fd4', '#61c0a8', '#f6bd16', '#f0884d', '#9270ca', '#5ad8a6'];
const GAP_X = 56;
const GAP_Y = 18;

function stripPrivate(shape) {
  const out = {};
  for (const [k, v] of Object.entries(shape)) if (!k.startsWith('_')) out[k] = v;
  return out;
}

export class MindmapController {
  constructor({ engine, crdt, shapes, tools = null, opts = {} }) {
    this.engine = engine;
    this.crdt = crdt;
    this.shapes = shapes;
    this.tools = tools;
    this.opts = opts;
    this.direction = opts.direction || 'right';
    this.selectedId = null;
    this.outlineEl = opts.outlineEl || null;
    this._relayoutTimer = null;
    engine.page = 'mindmap';
  }

  /* ------------------------------------------------------------ 树构建 */
  nodes() {
    return [...this.shapes.values()].filter((s) => s.kind === 'mindnode' && !s.deleted);
  }

  rootNode() {
    const all = this.nodes();
    return all.find((n) => !n.parent || !this.shapes.get(n.parent)
      || this.shapes.get(n.parent).deleted) || all[0] || null;
  }

  childrenOf(id) {
    return this.nodes()
      .filter((n) => n.parent === id)
      .sort((a, b) => (a.meta?.createdAt || 0) - (b.meta?.createdAt || 0)
        || String(a.id).localeCompare(String(b.id)));
  }

  levelOf(node) {
    let level = 0;
    let cursor = node?.parent;
    let guard = 0;
    while (cursor && guard < 100) {
      const parent = this.shapes.get(cursor);
      if (!parent || parent.deleted) break;
      level += 1;
      cursor = parent.parent;
      guard += 1;
    }
    return level;
  }

  /** 可见节点(祖先未折叠), DFS 顺序 */
  visibleDFS() {
    const out = [];
    const walk = (node, hidden) => {
      if (!node) return;
      if (!hidden) out.push(node);
      const nextHidden = hidden || !!node.collapsed;
      for (const child of this.childrenOf(node.id)) walk(child, nextHidden);
    };
    walk(this.rootNode(), false);
    return out;
  }

  subtreeIds(id) {
    const out = [];
    const walk = (nid) => {
      out.push(nid);
      for (const child of this.childrenOf(nid)) walk(child.id);
    };
    walk(id);
    return out;
  }

  /* ------------------------------------------------------------ 结构操作 */
  ensureRoot() {
    if (this.rootNode()) return this.rootNode();
    const root = createShape('mindnode', {
      id: uidShape(), x: 0, y: 0, text: '中心主题',
      fill: LEVEL_FILLS[0], textColor: '#ffffff', fontSize: 18,
      w: 190, h: 56, parent: null, author: this.opts.me?.username || '', z: nextZ(),
    });
    this.shapes.set(root.id, root);
    this.engine.gridIndex.insert(root);
    this.crdt.commit(this.shapes, [this.crdt.addShape(stripPrivate(root))], { label: '创建根节点' });
    this.select(root.id);
    return root;
  }

  addChild(parentId = this.selectedId) {
    const parent = this.shapes.get(parentId) || this.ensureRoot();
    if (!parent || parent.deleted) return null;
    const level = this.levelOf(parent) + 1;
    const size = mindNodeSize('新分支', level);
    const pb = bboxOf(parent);
    const node = createShape('mindnode', {
      id: uidShape(),
      x: pb.x1 + GAP_X, y: pb.y0 + pb.h / 2 - size.h / 2,
      text: '新分支', parent: parent.id,
      fill: LEVEL_FILLS[level % LEVEL_FILLS.length],
      textColor: level <= 1 ? '#ffffff' : '#1f2937',
      fontSize: size.fontSize, w: size.w, h: size.h,
      author: this.opts.me?.username || '', z: nextZ(),
    });
    if (parent.collapsed) {
      // 父节点折叠时, 添加子节点自动展开
      this.crdt.commit(this.shapes, [this.crdt.setProps(parent.id, { collapsed: false })], { label: '展开' });
    }
    this.shapes.set(node.id, node);
    this.engine.gridIndex.insert(node);
    this.crdt.commit(this.shapes, [this.crdt.addShape(stripPrivate(node))], { label: '添加子节点' });
    this.select(node.id);
    this.autoLayout();
    this.renderOutline();
    setTimeout(() => this.tools?.openTextEditor(node.id), 60);
    return node;
  }

  addSibling(nodeId = this.selectedId) {
    const node = this.shapes.get(nodeId);
    if (!node || node.deleted) return null;
    if (!node.parent) return this.addChild(node.id);        // 根节点 → 加子节点
    return this.addChild(node.parent);
  }

  removeNode(nodeId = this.selectedId) {
    const node = this.shapes.get(nodeId);
    if (!node || node.deleted) return;
    if (!node.parent) {
      if (this.nodes().length > 1) return;                  // 根节点有子树时保护
      this.crdt.commit(this.shapes, [this.crdt.deleteShape(node.id)], { label: '删除根节点' });
    } else {
      const ids = this.subtreeIds(nodeId);
      this.crdt.commit(this.shapes, ids.map((id) => this.crdt.deleteShape(id)), { label: '删除分支' });
      for (const id of ids) this.engine.gridIndex.removeById(id);
      this.select(node.parent);
    }
    this.autoLayout();
    this.renderOutline();
    this.engine.markDirty();
  }

  toggleCollapse(nodeId = this.selectedId) {
    const node = this.shapes.get(nodeId);
    if (!node || node.deleted || !this.childrenOf(nodeId).length) return;
    this.crdt.commit(this.shapes,
      [this.crdt.setProps(nodeId, { collapsed: !node.collapsed })],
      { label: node.collapsed ? '展开' : '折叠' });
    this.autoLayout();
    this.renderOutline();
    this.engine.markDirty();
  }

  collapseAll() {
    const ops = [];
    for (const node of this.nodes()) {
      if (node.parent && this.childrenOf(node.id).length && !node.collapsed) {
        ops.push(this.crdt.setProps(node.id, { collapsed: true }));
      }
    }
    if (ops.length) this.crdt.commit(this.shapes, ops, { label: '全部折叠' });
    this.autoLayout();
    this.renderOutline();
    this.engine.markDirty();
  }

  expandAll() {
    const ops = [];
    for (const node of this.nodes()) {
      if (node.collapsed) ops.push(this.crdt.setProps(node.id, { collapsed: false }));
    }
    if (ops.length) this.crdt.commit(this.shapes, ops, { label: '全部展开' });
    this.autoLayout();
    this.renderOutline();
    this.engine.markDirty();
  }

  renameSelected() {
    if (this.selectedId) this.tools?.openTextEditor(this.selectedId);
  }

  /* ------------------------------------------------------------ 自动布局 */
  /**
   * 整洁树布局: 计算目标位置 → 以 move 增量发布(一个撤销单元)。
   * @param {object} o {silent: 不平移相机}
   */
  autoLayout({ silent = false } = {}) {
    const root = this.rootNode();
    if (!root) return;
    const targets = new Map();
    let sink = targets;                 // layoutRight 的写入目标(both 模式换向局部表)
    let slotY = 0;                      // 下一个可用的纵向(或横向)槽位起点

    const sizeOf = (node) => {
      const level = this.levelOf(node);
      const s = mindNodeSize(node.text || '主题', level);
      return { w: Number(node.w) || s.w, h: Number(node.h) || s.h, level };
    };

    /** 向右布局: 返回节点中心 Y; 叶子按槽位排列, 内部节点取首末子节点中点 */
    const layoutRight = (node, x) => {
      const { w, h } = sizeOf(node);
      const children = node.collapsed ? [] : this.childrenOf(node.id);
      let centerY;
      if (!children.length) {
        centerY = slotY + h / 2;
        slotY += h + GAP_Y;
      } else {
        const childCenters = children.map((child) => layoutRight(child, x + w + GAP_X));
        centerY = (childCenters[0] + childCenters[childCenters.length - 1]) / 2;
        slotY = Math.max(slotY, centerY + h / 2 + GAP_Y);
      }
      sink.set(node.id, { x, y: centerY - h / 2, cy: centerY });
      return centerY;
    };

    /** 向下布局(组织架构): 返回节点中心 X */
    const layoutDown = (node, depth) => {
      const { w, h } = sizeOf(node);
      const children = node.collapsed ? [] : this.childrenOf(node.id);
      const y = depth * 130;
      let centerX;
      if (!children.length) {
        centerX = slotY + w / 2;
        slotY += w + GAP_X;
      } else {
        const childCenters = children.map((child) => layoutDown(child, depth + 1));
        centerX = (childCenters[0] + childCenters[childCenters.length - 1]) / 2;
        slotY = Math.max(slotY, centerX + w / 2 + GAP_X);
      }
      targets.set(node.id, { x: centerX - w / 2, y, cy: y + h / 2 });
      return centerX;
    };

    const rootSize = sizeOf(root);
    const rootW = Number(root.w) || rootSize.w;
    const rootH = Number(root.h) || rootSize.h;

    if (this.direction === 'down') {
      slotY = 0;
      layoutDown(root, 0);
      // 整体平移使根节点水平居中于 x=0
      const rootTarget = targets.get(root.id);
      const offsetX = -(rootTarget.x + rootW / 2);
      for (const t of targets.values()) t.x += offsetX;
    } else if (this.direction === 'both') {
      // 左右分布: 子节点交替分到右/左, 各自独立跑「向右布局」, 左半整体镜像
      targets.set(root.id, { x: -rootW / 2, y: -rootH / 2, cy: 0 });
      const children = root.collapsed ? [] : this.childrenOf(root.id);
      const right = []; const left = [];
      children.forEach((c, i) => (i % 2 === 0 ? right : left).push(c));
      const runSide = (list, mirror) => {
        if (!list.length) return;
        const local = new Map();
        sink = local;
        slotY = -(list.reduce((acc, c) => acc + this._subtreeHeight(c) + GAP_Y, 0) - GAP_Y) / 2;
        for (const child of list) layoutRight(child, rootW / 2 + GAP_X);
        sink = targets;
        for (const [id, t] of local) {
          if (mirror) {
            const w = Number(this.shapes.get(id)?.w) || 120;
            targets.set(id, { x: -(t.x + w), y: t.y, cy: t.cy });
          } else {
            targets.set(id, t);
          }
        }
      };
      runSide(right, false);
      runSide(left, true);
    } else {
      // 向右(默认): 根节点垂直居中
      slotY = -this._subtreeHeight(root) / 2;
      layoutRight(root, 0);
    }

    // 生成 move 增量
    const ops = [];
    for (const [id, t] of targets) {
      const shape = this.shapes.get(id);
      if (!shape || shape.deleted) continue;
      const dx = t.x - (Number(shape.x) || 0);
      const dy = t.y - (Number(shape.y) || 0);
      if (Math.abs(dx) > 0.5 || Math.abs(dy) > 0.5) {
        ops.push({ type: 'move', id, dx: Math.round(dx * 10) / 10, dy: Math.round(dy * 10) / 10 });
      }
    }
    if (ops.length) {
      const signed = ops.map((semantic) => this.crdt.move(semantic.id, semantic.dx, semantic.dy));
      this.crdt.commit(this.shapes, signed, { label: '自动布局' });
      for (const semantic of ops) this.engine.gridIndex.refresh(this.shapes.get(semantic.id));
      this.engine.markDirty();
    }
    if (!silent) {
      const rb = bboxOf(root);
      this.engine.centerOn((rb.x0 + rb.x1) / 2, (rb.y0 + rb.y1) / 2);
    }
  }

  _subtreeHeight(node) {
    const size = mindNodeSize(node.text || '主题', this.levelOf(node));
    const h = Number(node.h) || size.h;
    const children = node.collapsed ? [] : this.childrenOf(node.id);
    if (!children.length) return h;
    let total = 0;
    for (const child of children) total += this._subtreeHeight(child) + GAP_Y;
    return Math.max(h, total - GAP_Y);
  }

  /* ------------------------------------------------------------ 选择/导航 */
  select(id) {
    this.selectedId = id;
    this.engine.selection = new Set(id ? [id] : []);
    this.engine.markDirty('overlay');
    this.renderOutline();
  }

  focusNode(id) {
    const node = this.shapes.get(id);
    if (!node) return;
    const b = bboxOf(node);
    this.engine.centerOn((b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2);
    this.select(id);
  }

  navigate(key) {
    const list = this.visibleDFS();
    if (!list.length) return;
    let idx = list.findIndex((n) => n.id === this.selectedId);
    if (key === 'down') idx = Math.min(list.length - 1, idx + 1);
    else if (key === 'up') idx = Math.max(0, idx - 1);
    else if (key === 'right') {
      const node = list[idx];
      const children = node ? this.childrenOf(node.id) : [];
      if (children.length) { this.select(children[0].id); return; }
      return;
    } else if (key === 'left') {
      const node = list[idx];
      if (node?.parent) { this.select(node.parent); return; }
      return;
    }
    if (idx < 0) idx = 0;
    this.select(list[idx].id);
  }

  /* ------------------------------------------------------------ 大纲面板 */
  renderOutline() {
    const host = this.outlineEl;
    if (!host) return;
    const root = this.rootNode();
    if (!root) {
      host.innerHTML = '<div class="empty-state small" style="padding:24px 10px">暂无节点<br>按 <kbd>Tab</kbd> 或点击「子节点」开始</div>';
      return;
    }
    host.innerHTML = '';
    const build = (node, depth) => {
      const row = document.createElement('div');
      row.className = 'mm-outline-row';
      row.dataset.id = node.id;
      const children = this.childrenOf(node.id);
      const chevron = document.createElement('span');
      chevron.className = 'mm-chev';
      chevron.textContent = children.length ? (node.collapsed ? '▸' : '▾') : '·';
      if (children.length) {
        chevron.onclick = (e) => { e.stopPropagation(); this.toggleCollapse(node.id); };
      }
      const label = document.createElement('span');
      label.className = 'mm-label';
      label.textContent = node.text || '(未命名)';
      const dot = document.createElement('span');
      dot.className = 'mm-dot';
      dot.style.background = node.fill || LEVEL_FILLS[depth % LEVEL_FILLS.length];
      row.append(dot, chevron, label);
      row.style.paddingLeft = `${8 + depth * 16}px`;
      if (node.id === this.selectedId) row.classList.add('selected');
      row.onclick = () => this.focusNode(node.id);
      row.ondblclick = () => { this.focusNode(node.id); this.renameSelected(); };
      host.appendChild(row);
      if (!node.collapsed) children.forEach((child) => build(child, depth + 1));
    };
    build(root, 0);
  }

  /* ------------------------------------------------------------ 键盘 */
  installKeyboard() {
    window.addEventListener('keydown', (e) => {
      const t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      switch (e.key) {
        case 'Tab': e.preventDefault(); this.addChild(); break;
        case 'Enter': e.preventDefault(); this.addSibling(); break;
        case 'F2': e.preventDefault(); this.renameSelected(); break;
        case 'Delete': case 'Backspace': e.preventDefault(); this.removeNode(); break;
        case ' ': e.preventDefault(); this.toggleCollapse(); break;
        case 'ArrowDown': e.preventDefault(); this.navigate('down'); break;
        case 'ArrowUp': e.preventDefault(); this.navigate('up'); break;
        case 'ArrowLeft': e.preventDefault(); this.navigate('left'); break;
        case 'ArrowRight': e.preventDefault(); this.navigate('right'); break;
        default: break;
      }
    });
  }

  /* ------------------------------------------------------------ 远端事件 */
  /** 远端结构变化 → 只刷大纲/索引, 不抢布局(位置信任对方的 move 增量) */
  onRemoteStructuralChange() {
    clearTimeout(this._relayoutTimer);
    this._relayoutTimer = setTimeout(() => {
      if (this.selectedId) {
        const sel = this.shapes.get(this.selectedId);
        if (!sel || sel.deleted) this.select(null);
      }
      this.renderOutline();
    }, 120);
  }

  setDirection(direction) {
    this.direction = direction;
    this.autoLayout();
    this.renderOutline();
  }
}

export { LEVEL_FILLS, GAP_X, GAP_Y };
