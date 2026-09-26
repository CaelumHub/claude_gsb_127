/* ================================================================
   ui.js —— 共享 UI 基础设施: 顶栏导航 / 主题 / Toast / 模态框 /
   登录门禁 / 用户信息 / 通用工具函数
   ================================================================ */
import { Api } from './api.js';

export const PAGES = [
  { id: 'index',       href: '/index.html',       label: '白板列表' },
  { id: 'editor',      href: '/editor.html',      label: '白板编辑' },
  { id: 'mindmap',     href: '/mindmap.html',     label: '思维导图' },
  { id: 'files',       href: '/files.html',       label: '文件管理' },
  { id: 'chat',        href: '/chat.html',        label: '协作聊天' },
  { id: 'replay',      href: '/replay.html',      label: '历史回放' },
  { id: 'permissions', href: '/permissions.html', label: '用户权限' },
  { id: 'templates',   href: '/templates.html',   label: '模板库' },
  { id: 'export',      href: '/export.html',      label: '导出' },
  { id: 'settings',    href: '/settings.html',    label: '系统设置' },
];

/* ---------------------------------------------------------------- 工具函数 */
export function uid(prefix = 'id') {
  const rand = (crypto && crypto.randomUUID)
    ? crypto.randomUUID().replace(/-/g, '').slice(0, 12)
    : Math.random().toString(36).slice(2, 14);
  return `${prefix}_${rand}`;
}

export function nowMs() { return Date.now(); }

export function debounce(fn, ms = 250) {
  let timer = null;
  return function (...args) {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), ms);
  };
}

export function throttle(fn, ms = 100) {
  let last = 0; let timer = null;
  return function (...args) {
    const now = Date.now();
    const remain = ms - (now - last);
    if (remain <= 0) {
      last = now;
      fn.apply(this, args);
    } else if (!timer) {
      timer = setTimeout(() => { timer = null; last = Date.now(); fn.apply(this, args); }, remain);
    }
  };
}

export function fmtTime(ts, withSec = false) {
  if (!ts) return '—';
  const d = new Date(ts);
  const p = (n) => String(n).padStart(2, '0');
  const base = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  return withSec ? `${base}:${p(d.getSeconds())}` : base;
}

export function fmtRel(ts) {
  if (!ts) return '—';
  const diff = Date.now() - ts;
  if (diff < 60_000) return '刚刚';
  if (diff < 3600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86400_000) return `${Math.floor(diff / 3600_000)} 小时前`;
  if (diff < 7 * 86400_000) return `${Math.floor(diff / 86400_000)} 天前`;
  return fmtTime(ts);
}

export function fmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

export function fmtNum(n) {
  if (n == null) return '—';
  if (n >= 10000) return `${(n / 10000).toFixed(1)} 万`;
  return String(n);
}

export function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

export function initials(name = '?') {
  const s = String(name).trim();
  if (!s) return '?';
  if (/^[一-鿿]/.test(s)) return s.slice(-2);
  return s.slice(0, 2).toUpperCase();
}

export const USER_COLORS = ['#5b8ff9', '#61c0a8', '#f0884d', '#d4709c', '#9270ca',
  '#5ad8a6', '#f6bd16', '#e8684a', '#6dc8ec', '#ff9d4d'];

export function colorOf(name = '') {
  let h = 0;
  for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return USER_COLORS[h % USER_COLORS.length];
}

export function qs(name) {
  return new URLSearchParams(location.search).get(name);
}

/* ---------------------------------------------------------------- 主题 */
export function getTheme() {
  try { return localStorage.getItem('wb_theme') || 'dark'; } catch { return 'dark'; }
}

export function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  try { localStorage.setItem('wb_theme', theme); } catch { /* 隐私模式 */ }
  window.dispatchEvent(new CustomEvent('wb:theme', { detail: { theme } }));
}

export function toggleTheme() {
  applyTheme(getTheme() === 'dark' ? 'light' : 'dark');
}

/* ---------------------------------------------------------------- Toast */
export function toast(message, type = 'info', ms = 2800) {
  let host = document.getElementById('wb-toasts');
  if (!host) {
    host = document.createElement('div');
    host.id = 'wb-toasts';
    document.body.appendChild(host);
  }
  const el = document.createElement('div');
  el.className = `wb-toast ${type}`;
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => {
    el.classList.add('out');
    setTimeout(() => el.remove(), 300);
  }, ms);
  return el;
}

/* ---------------------------------------------------------------- 模态框 */
export function modal({ title = '', body = '', footer = '', width = '', onMount = null }) {
  const mask = document.createElement('div');
  mask.className = 'modal-mask';
  mask.innerHTML = `
    <div class="modal ${width}" role="dialog">
      <div class="modal-head">${escapeHtml(title)}<span class="x" title="关闭">×</span></div>
      <div class="modal-body">${body}</div>
      ${footer ? `<div class="modal-foot">${footer}</div>` : ''}
    </div>`;
  const close = () => mask.remove();
  mask.querySelector('.x').addEventListener('click', close);
  mask.addEventListener('mousedown', (e) => { if (e.target === mask) close(); });
  document.body.appendChild(mask);
  if (onMount) onMount(mask, close);
  return { el: mask, close };
}

export function confirmDialog(message, { title = '确认操作', danger = false } = {}) {
  return new Promise((resolve) => {
    const m = modal({
      title,
      body: `<div style="font-size:14px;color:var(--text-soft)">${escapeHtml(message)}</div>`,
      footer: `<button class="btn" data-no>取消</button>
               <button class="btn ${danger ? 'danger' : 'primary'}" data-yes>确定</button>`,
      onMount(el, close) {
        el.querySelector('[data-no]').onclick = () => { close(); resolve(false); };
        el.querySelector('[data-yes]').onclick = () => { close(); resolve(true); };
      },
    });
    m.el.addEventListener('mousedown', (e) => { if (e.target === m.el) resolve(false); });
  });
}

export function promptDialog(message, { title = '请输入', value = '', placeholder = '' } = {}) {
  return new Promise((resolve) => {
    const m = modal({
      title,
      body: `<div class="field"><label>${escapeHtml(message)}</label>
             <input class="input" data-input value="${escapeHtml(value)}" placeholder="${escapeHtml(placeholder)}"></div>`,
      footer: `<button class="btn" data-no>取消</button><button class="btn primary" data-ok>确定</button>`,
      onMount(el, close) {
        const input = el.querySelector('[data-input]');
        setTimeout(() => input.focus(), 30);
        const ok = () => { close(); resolve(input.value.trim() || null); };
        el.querySelector('[data-ok]').onclick = ok;
        el.querySelector('[data-no]').onclick = () => { close(); resolve(null); };
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') ok(); });
      },
    });
    m.el.addEventListener('mousedown', (e) => { if (e.target === m.el) resolve(null); });
  });
}

/* ---------------------------------------------------------------- 用户态 */
let _me = null;
export function getMe() { return _me; }
export function setMe(user) {
  _me = user;
  window.dispatchEvent(new CustomEvent('wb:me', { detail: { user } }));
  renderUserChip();
}

export async function loadMe() {
  const token = Api.token;
  if (!token) { setMe(null); return null; }
  try {
    const data = await Api.auth.me();
    setMe(data.user);
    return data.user;
  } catch {
    Api.clearToken();
    setMe(null);
    return null;
  }
}

export function avatarHtml(user, cls = '') {
  const name = user?.display_name || user?.username || '?';
  const color = user?.color || colorOf(name);
  return `<span class="wb-avatar ${cls}" style="background:${color}" title="${escapeHtml(name)}">${escapeHtml(initials(name))}</span>`;
}

/* ---------------------------------------------------------------- 登录框 */
export function loginDialog(reason = '') {
  return new Promise((resolve) => {
    const m = modal({
      title: '登录 / 注册',
      width: 'auth-gate',
      body: `
        ${reason ? `<div class="mb16 small" style="color:var(--orange)">${escapeHtml(reason)}</div>` : ''}
        <div class="field"><label>用户名</label><input class="input" data-user placeholder="demo" autocomplete="username"></div>
        <div class="field"><label>密码</label><input class="input" data-pass type="password" placeholder="demo123" autocomplete="current-password"></div>
        <div class="field hidden" data-regwrap>
          <label>昵称(注册用, 可选)</label><input class="input" data-nick placeholder="我的昵称">
        </div>
        <div class="row small muted">
          <span>演示账号: demo / demo123 · alice / alice123 · bob / bob123</span>
        </div>`,
      footer: `<button class="btn" data-reg>注册新账号</button>
               <span class="spacer"></span>
               <button class="btn ghost" data-cancel>稍后再说</button>
               <button class="btn primary" data-login>登录</button>`,
      onMount(el, close) {
        const userInput = el.querySelector('[data-user]');
        const passInput = el.querySelector('[data-pass]');
        setTimeout(() => userInput.focus(), 40);
        el.querySelector('[data-cancel]').onclick = () => { close(); resolve(null); };
        el.querySelector('[data-reg]').onclick = async () => {
          const nickWrap = el.querySelector('[data-regwrap]');
          nickWrap.classList.remove('hidden');
          try {
            const data = await Api.auth.register(userInput.value.trim(), passInput.value,
              el.querySelector('[data-nick]').value.trim() || undefined);
            Api.setToken(data.token);
            await loadMe();
            close(); resolve(getMe());
          } catch (err) { toast(err.message || '注册失败', 'error'); }
        };
        const doLogin = async () => {
          try {
            const data = await Api.auth.login(userInput.value.trim(), passInput.value);
            Api.setToken(data.token);
            await loadMe();
            close(); resolve(getMe());
          } catch (err) { toast(err.message || '登录失败', 'error'); }
        };
        el.querySelector('[data-login]').onclick = doLogin;
        passInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });
      },
    });
    m.el.addEventListener('mousedown', (e) => { if (e.target === m.el) resolve(null); });
  });
}

/** 页面级登录门禁: 未登录时弹登录框; 返回 me 或 null。 */
export async function requireAuth(reason = '请先登录后使用') {
  let me = await loadMe();
  if (!me) me = await loginDialog(reason);
  return me;
}

export async function logout() {
  try { await Api.auth.logout(); } catch { /* 忽略 */ }
  Api.clearToken();
  setMe(null);
  toast('已退出登录');
}

/* ---------------------------------------------------------------- 顶栏 */
function renderUserChip() {
  const host = document.getElementById('wb-user-slot');
  if (!host) return;
  if (_me) {
    host.innerHTML = `
      <span class="wb-userchip" id="wb-chip">
        ${avatarHtml(_me)}
        <span>${escapeHtml(_me.display_name || _me.username)}</span>
        ${_me.role === 'admin' ? '<span class="badge purple" style="padding:0 6px">管理</span>' : ''}
      </span>`;
    host.querySelector('#wb-chip').onclick = () => userMenu(host);
  } else {
    host.innerHTML = `<button class="btn primary sm" id="wb-login-btn">登录</button>`;
    host.querySelector('#wb-login-btn').onclick = () => loginDialog();
  }
}

function userMenu(host) {
  const existing = document.getElementById('wb-usermenu');
  if (existing) { existing.remove(); return; }
  const menu = document.createElement('div');
  menu.id = 'wb-usermenu';
  menu.className = 'panel';
  menu.style.cssText = 'position:fixed;z-index:950;min-width:170px;padding:6px;box-shadow:var(--shadow)';
  const rect = host.getBoundingClientRect();
  menu.style.top = `${rect.bottom + 6}px`;
  menu.style.right = `${window.innerWidth - rect.right}px`;
  menu.innerHTML = `
    <div style="padding:8px 10px 6px;border-bottom:1px solid var(--border-soft);margin-bottom:4px">
      <div class="bold">${escapeHtml(_me?.display_name || '')}</div>
      <div class="small muted">@${escapeHtml(_me?.username || '')} · ${_me?.role === 'admin' ? '管理员' : '普通用户'}</div>
    </div>
    <a class="menu-item" href="/settings.html">⚙️ 系统设置</a>
    <a class="menu-item" href="#" data-theme>🌓 切换主题</a>
    <a class="menu-item" href="#" data-logout style="color:var(--red)">🚪 退出登录</a>`;
  menu.querySelectorAll('.menu-item').forEach((el) => {
    el.style.cssText = 'display:block;padding:7px 10px;border-radius:6px;color:var(--text-soft);font-size:13px';
    el.onmouseenter = () => { el.style.background = 'var(--bg-hover)'; };
    el.onmouseleave = () => { el.style.background = ''; };
  });
  menu.querySelector('[data-theme]').onclick = (e) => { e.preventDefault(); toggleTheme(); menu.remove(); };
  menu.querySelector('[data-logout]').onclick = async (e) => { e.preventDefault(); await logout(); location.reload(); };
  document.body.appendChild(menu);
  setTimeout(() => {
    const away = (e) => { if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener('mousedown', away); } };
    document.addEventListener('mousedown', away);
  }, 10);
}

/** 挂载统一顶栏。active = PAGES 里的 id。 */
export function mountNav(active = '') {
  if (document.getElementById('wb-topbar')) return;
  const bar = document.createElement('header');
  bar.className = 'wb-topbar';
  bar.id = 'wb-topbar';
  bar.innerHTML = `
    <a class="wb-logo" href="/index.html"><span class="logo-mark">协</span><span>CoBoard 协同白板</span></a>
    <nav class="wb-nav">${PAGES.map((p) => `
      <a href="${p.href}" class="${p.id === active ? 'active' : ''}">${p.label}</a>`).join('')}
    </nav>
    <div class="wb-topbar-right">
      <button class="btn ghost icon sm" id="wb-theme-btn" title="切换主题">🌓</button>
      <span id="wb-user-slot"></span>
    </div>`;
  document.body.prepend(bar);
  bar.querySelector('#wb-theme-btn').onclick = toggleTheme;
  applyTheme(getTheme());
  renderUserChip();
}

/** 标准页面初始化: 主题 + 顶栏 + 登录态。options.gate=true 时强制登录。 */
export async function initPage(active, { gate = false, title = '' } = {}) {
  applyTheme(getTheme());
  mountNav(active);
  if (title) document.title = `${title} · CoBoard`;
  const me = gate ? await requireAuth() : await loadMe();
  if (gate && !me) {
    document.querySelector('.wb-main, .wb-full')?.replaceWith(Object.assign(
      document.createElement('div'),
      { className: 'empty-state', innerHTML: '<div class="icon">🔒</div><div class="title">需要登录</div><div>刷新页面重新登录</div>' },
    ));
  }
  return me;
}
