/* ================================================================
   api.js —— REST 客户端封装
   token 存 localStorage(wb_token); 所有请求带 X-Auth-Token 头;
   401 时清 token 并广播 wb:unauthorized 事件(页面自行弹登录)。
   ================================================================ */

const TOKEN_KEY = 'wb_token';

export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

async function request(path, { method = 'GET', body = null, raw = false, params = null } = {}) {
  const url = new URL(path, location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, String(v));
    }
  }
  const headers = {};
  const token = Api.token;
  if (token) headers['X-Auth-Token'] = token;
  if (body !== null && body !== undefined) headers['Content-Type'] = 'application/json';
  const resp = await fetch(url, {
    method,
    headers,
    body: body !== null && body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (resp.status === 401) {
    Api.clearToken();
    window.dispatchEvent(new CustomEvent('wb:unauthorized'));
  }
  if (raw) {
    if (!resp.ok) throw new ApiError(`请求失败(${resp.status})`, resp.status);
    return resp;
  }
  let data = null;
  try { data = await resp.json(); } catch { /* 204 等 */ }
  if (!resp.ok) {
    const message = (data && (data.detail || data.message)) || `请求失败(HTTP ${resp.status})`;
    throw new ApiError(typeof message === 'string' ? message : JSON.stringify(message), resp.status, data);
  }
  return data;
}

export const Api = {
  get token() {
    try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
  },
  setToken(token) {
    try { localStorage.setItem(TOKEN_KEY, token); } catch { /* 忽略 */ }
  },
  clearToken() {
    try { localStorage.removeItem(TOKEN_KEY); } catch { /* 忽略 */ }
  },

  /* 拼接 WS 地址 */
  wsUrl(boardId, { clientId = '', lastRev = 0, page = 'editor' } = {}) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = new URL(`${proto}://${location.host}/ws/${encodeURIComponent(boardId)}`);
    if (Api.token) url.searchParams.set('token', Api.token);
    if (clientId) url.searchParams.set('client_id', clientId);
    url.searchParams.set('last_rev', String(lastRev || 0));
    url.searchParams.set('page', page);
    return url.toString();
  },

  download(url, filename) {
    const a = document.createElement('a');
    a.href = url;
    if (filename) a.download = filename;
    a.target = '_blank';
    document.body.appendChild(a);
    a.click();
    a.remove();
  },

  /* ------------------------------------------------------------ 认证 */
  auth: {
    register: (username, password, displayName) => request('/api/auth/register', { method: 'POST', body: { username, password, display_name: displayName } }),
    login: (username, password) => request('/api/auth/login', { method: 'POST', body: { username, password } }),
    logout: () => request('/api/auth/logout', { method: 'POST' }),
    me: () => request('/api/auth/me'),
  },

  /* ------------------------------------------------------------ 用户 */
  users: {
    list: () => request('/api/users'),
    patch: (username, patch) => request(`/api/users/${encodeURIComponent(username)}`, { method: 'PATCH', body: patch }),
  },

  /* ------------------------------------------------------------ 白板 */
  boards: {
    list: (params) => request('/api/boards', { params }),
    create: (payload) => request('/api/boards', { method: 'POST', body: payload }),
    get: (id) => request(`/api/boards/${id}`),
    patch: (id, payload) => request(`/api/boards/${id}`, { method: 'PATCH', body: payload }),
    remove: (id) => request(`/api/boards/${id}`, { method: 'DELETE' }),
    duplicate: (id, name) => request(`/api/boards/${id}/duplicate`, { method: 'POST', body: { name } }),
    state: (id) => request(`/api/boards/${id}/state`),
    snapshot: (id) => request(`/api/boards/${id}/snapshot`, { method: 'POST' }),
    stats: (id) => request(`/api/boards/${id}/stats`),
    permissions: (id) => request(`/api/boards/${id}/permissions`),
    putPermissions: (id, acl, publicRole) => request(`/api/boards/${id}/permissions`, { method: 'PUT', body: { acl, public_role: publicRole } }),
    exportUrl: (id, format, opts = {}) => {
      const url = new URL(`/api/boards/${id}/export`, location.origin);
      url.searchParams.set('format', format);
      if (opts.rev != null) url.searchParams.set('rev', opts.rev);
      if (opts.background) url.searchParams.set('background', opts.background);
      if (opts.grid) url.searchParams.set('grid', 'true');
      if (Api.token) url.searchParams.set('token', Api.token);
      return url.toString();
    },
  },

  /* ------------------------------------------------------------ 历史/回放 */
  history: {
    index: (id) => request(`/api/boards/${id}/history/index`),
    ops: (id, params) => request(`/api/boards/${id}/history/ops`, { params }),
    replay: (id, params) => request(`/api/boards/${id}/history/replay`, { params }),
    compact: (id) => request(`/api/boards/${id}/history/compact`, { method: 'POST', body: { confirm: true } }),
    prune: (id) => request(`/api/boards/${id}/history/prune`, { method: 'POST' }),
  },

  /* ------------------------------------------------------------ 聊天 */
  chat: {
    list: (id, params) => request(`/api/boards/${id}/chat`, { params }),
    post: (id, text) => request(`/api/boards/${id}/chat`, { method: 'POST', body: { text } }),
  },

  /* ------------------------------------------------------------ 模板 */
  templates: {
    list: () => request('/api/templates'),
    shapes: (id) => request(`/api/templates/${id}/shapes`),
    create: (payload) => request('/api/templates', { method: 'POST', body: payload }),
    remove: (id) => request(`/api/templates/${id}`, { method: 'DELETE' }),
  },

  /* ------------------------------------------------------------ 设置/系统 */
  settings: {
    get: () => request('/api/settings'),
    put: (settings) => request('/api/settings', { method: 'PUT', body: { settings } }),
  },
  system: {
    health: () => request('/api/system/health'),
    stats: () => request('/api/system/stats'),
  },
};

export { request };
