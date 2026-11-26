'use strict';

const TAB_TITLES = {
  news: '1. 资讯管理',
  articles: '2. 文章管理',
  publish: '3. 发布记录',
  settings: '4. 系统配置'
};

const DEFAULT_SITE_NAME = '资讯后台管理系统';

const STAGE_LABELS = {
  win_collect: 'Windows 采集',
  win_produce: 'Windows 生产',
  win_sync: 'Windows 同步',
  ingest: 'Debian 入库',
  news_publish: '资讯翻译推送',
  wechat_draft: '微信草稿推送',
  draft_retry: '草稿自动重试',
  publish_record: '人工发布确认',
  cleanup: '空间清理',
  backup: '数据库备份',
  settings: '系统配置',
  auth: '身份认证'
};

const PAGE_LIMIT = 50;

const pagination = {
  news: { offset: 0, total: 0, limit: PAGE_LIMIT },
  articles: { offset: 0, total: 0, limit: PAGE_LIMIT },
  publish: { offset: 0, total: 0, limit: PAGE_LIMIT },
  logs: { offset: 0, total: 0, limit: PAGE_LIMIT }
};

let currentTab = 'news';
let authState = {
  authenticated: false,
  auth_enabled: true,
  username: 'admin',
  wechat_configured: false
};
let modalArticle = null;
let modalViewMode = 'html';
let pendingPublishRecordId = null;

// ---------- Utilities ----------
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, m => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[m]));
}

function safeUrl(u) {
  const raw = String(u ?? '').trim();
  if (!raw) return '#';
  if (raw.startsWith('/') || /^https?:\/\//i.test(raw)) return raw;
  return '#';
}

function formatBytes(bytes) {
  const n = Number(bytes || 0);
  if (!Number.isFinite(n) || n <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let idx = 0;
  let val = n;
  while (val >= 1024 && idx < units.length - 1) {
    val /= 1024;
    idx++;
  }
  return `${idx === 0 ? Math.round(val) : val.toFixed(1)} ${units[idx]}`;
}

function toast(msg, kind = 'info') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast' + (kind === 'error' ? ' toast-error' : kind === 'success' ? ' toast-success' : '');
  el.style.display = 'block';
  clearTimeout(window._toastTimer);
  window._toastTimer = setTimeout(() => { el.style.display = 'none'; }, 3800);
}

function setPasswordVisibility(inputId, btn, visible) {
  const input = document.getElementById(inputId);
  if (!input) return;
  input.type = visible ? 'text' : 'password';
  if (btn) {
    btn.setAttribute('aria-pressed', visible ? 'true' : 'false');
    const eyeOpen = btn.querySelector('.icon-eye-open');
    const eyeClosed = btn.querySelector('.icon-eye-closed');
    if (eyeOpen) eyeOpen.classList.toggle('hidden', visible);
    if (eyeClosed) eyeClosed.classList.toggle('hidden', !visible);
  }
}

function toggleInputPassword(inputId, btn) {
  const input = document.getElementById(inputId);
  if (!input) return;
  const show = input.type === 'password';
  setPasswordVisibility(inputId, btn, show);
}

function isSecurityAlert(category) {
  return String(category || '').includes('安全预警');
}

function categoryBadge(category) {
  const label = category || 'AI新闻';
  if (isSecurityAlert(label)) {
    return `<span class="badge badge-alert">⚠ ${esc(label)}</span>`;
  }
  return `<span class="badge badge-gray">${esc(label)}</span>`;
}

function statusBadge(st) {
  const map = {
    candidate: ['待选', 'badge-gray'],
    selected: ['已选用', 'badge-blue'],
    ignored: ['已忽略', 'badge-gray'],
    ready: ['待推草稿', 'badge-yellow'],
    draft_uploaded: ['已入草稿箱', 'badge-blue'],
    draft_failed: ['推草稿失败', 'badge-red'],
    published_manual: ['已人工发布', 'badge-green'],
    success: ['成功', 'badge-green'],
    failed: ['失败', 'badge-red'],
    queued: ['自动重试排队中', 'badge-yellow'],
    draft_ready: ['草稿箱待发布', 'badge-blue'],
    manual_published: ['已人工发布', 'badge-green'],
    completed: ['已完成', 'badge-green'],
    ok: ['正常', 'badge-green'],
    warning: ['警告', 'badge-yellow']
  };
  const [label, cls] = map[st] || [st || '-', 'badge-gray'];
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}

function updatePaginationUI(prefix, state) {
  const total = Math.max(0, Number(state.total || 0));
  const limit = Math.max(1, Number(state.limit || PAGE_LIMIT));
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const currentPage = Math.min(totalPages, Math.floor(state.offset / limit) + 1);

  const infoEl = document.getElementById(`${prefix}-page-info`);
  if (infoEl) {
    infoEl.textContent = `第 ${currentPage} / ${totalPages} 页 · 共 ${total} 条`;
  }
  const prevBtn = document.getElementById(`${prefix}-prev-btn`);
  if (prevBtn) {
    prevBtn.disabled = state.offset <= 0;
  }
  const nextBtn = document.getElementById(`${prefix}-next-btn`);
  if (nextBtn) {
    nextBtn.disabled = state.offset + limit >= total;
  }
}

function buildWechatSrcdoc(htmlBody) {
  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><style>
    body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; color: #1f2937; line-height: 1.78; font-size: 15px; max-width: 677px; margin: 0 auto; padding: 16px 18px 36px; word-wrap: break-word; }
    img { max-width: 100%; height: auto; border-radius: 6px; }
    table { width: 100%; border-collapse: collapse; margin: 14px 0; font-size: 13px; }
    th, td { border: 1px solid #e2e8f0; padding: 8px; text-align: left; }
    th { background: #f8fafc; }
    blockquote { border-left: 4px solid #2563eb; background: #f8fafc; padding: 10px 14px; margin: 12px 0; color: #334155; }
    pre { background: #0f172a; color: #e2e8f0; padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 13px; }
    code { background: #f1f5f9; padding: 2px 5px; border-radius: 4px; font-size: 90%; }
    pre code { background: transparent; padding: 0; }
  </style></head><body>${htmlBody || '<p>暂无 HTML 正文内容</p>'}</body></html>`;
}

// ---------- API Client with 401 Session Handling ----------
async function api(url, opts = {}) {
  const { skipAuthRedirect, headers, ...rest } = opts;
  const res = await fetch(url, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(headers || {}) },
    ...rest
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !skipAuthRedirect) {
    handleUnauthorized(data.detail || '登录状态已失效，请重新登录');
    const err = new Error(data.detail || '未登录或登录已过期');
    err.isAuth = true;
    throw err;
  }
  if (!res.ok) {
    throw new Error(data.detail || `HTTP ${res.status}`);
  }
  return data;
}

// ---------- View Transitions ----------
function showBoot() {
  document.getElementById('boot-screen').classList.remove('hidden');
  document.getElementById('login-screen').classList.add('hidden');
  document.getElementById('onboarding-screen').classList.add('hidden');
  document.getElementById('app-shell').classList.add('hidden');
}

function showLoginScreen(errorMsg = '') {
  closeArticleModal();
  closePublishModal();
  closeLogsDrawer();
  closeMobileSidebar();
  document.getElementById('boot-screen').classList.add('hidden');
  document.getElementById('onboarding-screen').classList.add('hidden');
  document.getElementById('app-shell').classList.add('hidden');
  document.getElementById('login-screen').classList.remove('hidden');

  const errEl = document.getElementById('login-error');
  if (errorMsg) {
    errEl.textContent = errorMsg;
    errEl.classList.remove('hidden');
  } else {
    errEl.textContent = '';
    errEl.classList.add('hidden');
  }
  setPasswordVisibility('login-password', document.getElementById('login-pwd-toggle'), false);
  const pwdInput = document.getElementById('login-password');
  if (pwdInput) {
    pwdInput.value = '';
    setTimeout(() => pwdInput.focus(), 40);
  }
}

function handleUnauthorized(msg) {
  authState.authenticated = false;
  showLoginScreen(msg);
}

function showOnboardingScreen() {
  document.getElementById('boot-screen').classList.add('hidden');
  document.getElementById('login-screen').classList.add('hidden');
  document.getElementById('app-shell').classList.add('hidden');
  document.getElementById('onboarding-screen').classList.remove('hidden');
  document.getElementById('onboarding-error').classList.add('hidden');
  document.getElementById('ob-real-mode').checked = true;
  setPasswordVisibility('ob-secret', document.getElementById('ob-secret-toggle'), false);
  setTimeout(() => document.getElementById('ob-appid').focus(), 40);
}

function applySiteBranding(siteName) {
  const title = String(siteName || '').trim() || DEFAULT_SITE_NAME;
  const titleEl = document.getElementById('site-title');
  if (titleEl) titleEl.textContent = title;
  document.title = title;
}

function isLogsDrawerOpen() {
  const drawer = document.getElementById('logs-drawer');
  return Boolean(drawer && drawer.classList.contains('open'));
}

async function loadActiveTabData({ prefetchedConfig = null } = {}) {
  if (currentTab === 'news') return loadNews(false);
  if (currentTab === 'articles') return loadArticles(false);
  if (currentTab === 'publish') return loadPublishTab(false);
  if (currentTab === 'settings') return loadSettingsAndBackups(prefetchedConfig);
}

async function refreshWorkspaceAfterChange() {
  const tasks = [
    refreshHealthBar(),
    refreshLogAlertDot(),
    loadActiveTabData()
  ];
  if (isLogsDrawerOpen()) tasks.push(loadLogs(false));
  await Promise.allSettled(tasks);
}

async function enterMainApp(prefetchedConfig = null) {
  document.getElementById('boot-screen').classList.add('hidden');
  document.getElementById('login-screen').classList.add('hidden');
  document.getElementById('onboarding-screen').classList.add('hidden');
  document.getElementById('app-shell').classList.remove('hidden');
  document.getElementById('sidebar-user-info').textContent = `账号：${authState.username || 'admin'}`;

  let cfg = prefetchedConfig;
  if (!cfg) {
    try {
      cfg = await api('/api/config');
    } catch (e) {
      if (e && e.isAuth) return;
      applySiteBranding(DEFAULT_SITE_NAME);
    }
  }
  if (cfg) {
    applySiteBranding(cfg.site_name);
    if (typeof cfg.wechat_configured === 'boolean') {
      authState.wechat_configured = cfg.wechat_configured;
    }
  }
  await refreshCurrentTab({ prefetchedConfig: cfg });
}

// ---------- Auth & Onboarding Handlers ----------
document.getElementById('login-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const errEl = document.getElementById('login-error');
  const btn = document.getElementById('login-submit');
  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;
  const remember = document.getElementById('login-remember').checked;

  if (!username || !password) {
    errEl.textContent = '请输入用户名和密码';
    errEl.classList.remove('hidden');
    return;
  }

  errEl.classList.add('hidden');
  btn.disabled = true;
  btn.textContent = '登录中...';

  try {
    const res = await api('/api/auth/login', {
      method: 'POST',
      skipAuthRedirect: true,
      body: JSON.stringify({ username, password, remember })
    });
    authState.authenticated = true;
    authState.username = res.username || username;
    authState.wechat_configured = Boolean(res.wechat_configured);
    document.getElementById('login-password').value = '';

    if (!authState.wechat_configured) {
      showOnboardingScreen();
    } else {
      await enterMainApp();
    }
  } catch (e) {
    errEl.textContent = e.message || '用户名或密码错误';
    errEl.classList.remove('hidden');
  } finally {
    btn.disabled = false;
    btn.textContent = '登录后台';
  }
});

document.getElementById('onboarding-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const errEl = document.getElementById('onboarding-error');
  const btn = document.getElementById('ob-save-btn');
  const appid = document.getElementById('ob-appid').value.trim();
  const secret = document.getElementById('ob-secret').value.trim();
  const realMode = document.getElementById('ob-real-mode').checked;

  if (!appid || !secret) {
    errEl.textContent = '请完整填写微信 AppID 和 AppSecret（或点击左下角稍后填写）';
    errEl.classList.remove('hidden');
    return;
  }

  errEl.classList.add('hidden');
  btn.disabled = true;
  btn.textContent = '正在保存...';

  try {
    const cfg = await api('/api/config', {
      method: 'PUT',
      body: JSON.stringify({
        wechat_appid: appid,
        wechat_appsecret: secret,
        wechat_mock_mode: !realMode
      })
    });
    authState.wechat_configured = Boolean(cfg.wechat_configured ?? true);
    document.getElementById('ob-secret').value = '';
    toast('微信推送配置已保存，已进入管理后台', 'success');
    await enterMainApp(cfg);
  } catch (e) {
    if (e && e.isAuth) return;
    errEl.textContent = '保存失败：' + e.message;
    errEl.classList.remove('hidden');
  } finally {
    btn.disabled = false;
    btn.textContent = '保存并进入后台';
  }
});

document.getElementById('ob-skip-btn').addEventListener('click', async () => {
  await enterMainApp();
});

async function logout() {
  try {
    await api('/api/auth/logout', { method: 'POST', skipAuthRedirect: true });
  } catch {}
  authState.authenticated = false;
  if (authState.auth_enabled) {
    showLoginScreen('');
    toast('已安全退出登录', 'info');
  } else {
    toast('当前未启用密码校验（ADMIN_PASSWORD 未设置）', 'info');
  }
}

// ---------- Navigation & Mobile Sidebar ----------
function openMobileSidebar() {
  document.getElementById('sidebar').classList.add('mobile-open');
  document.getElementById('sidebar-backdrop').classList.add('open');
}

function closeMobileSidebar() {
  document.getElementById('sidebar').classList.remove('mobile-open');
  document.getElementById('sidebar-backdrop').classList.remove('open');
}

document.querySelectorAll('.nav-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    closeMobileSidebar();
    switchTab(btn.dataset.tab);
  });
});

function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `tab-${tab}`));
  document.getElementById('page-title').textContent = TAB_TITLES[tab] || tab;
  refreshCurrentTab();
}

// ---------- Topbar Health, Disk & Log Alert Dot ----------
async function refreshHealthBar() {
  try {
    const h = await api('/health', { skipAuthRedirect: true });
    const pill = document.getElementById('health-pill');
    const dot = document.getElementById('health-dot');
    const txt = document.getElementById('health-text');
    pill.className = 'pill pill-green';
    dot.className = 'dot dot-green';
    txt.textContent = `正常 ${(h.time || '').slice(11, 16)}`;

    const diskHuman = h.disk_usage_human || (h.disk_usage_bytes != null ? formatBytes(h.disk_usage_bytes) : '--');
    document.getElementById('disk-pill').textContent = `磁盘 ${diskHuman}`;

    const walInfo = document.getElementById('sidebar-wal-info');
    if (walInfo) {
      const mode = h.sqlite ? String(h.sqlite.journal_mode || 'wal').toUpperCase() : 'WAL';
      const retain = Number(h.data_retain_days || 7);
      walInfo.textContent = `SQLite: ${mode} · 保留 ${retain} 天`;
    }

    const retryBadge = document.getElementById('nav-retry-badge');
    if (retryBadge) {
      const q = Number(h.queued_drafts || 0);
      if (q > 0) {
        retryBadge.textContent = String(q);
        retryBadge.classList.remove('hidden');
      } else {
        retryBadge.classList.add('hidden');
      }
    }
  } catch {
    const pill = document.getElementById('health-pill');
    const dot = document.getElementById('health-dot');
    const txt = document.getElementById('health-text');
    pill.className = 'pill pill-red';
    dot.className = 'dot dot-red';
    txt.textContent = '连接异常';
  }
}

async function refreshLogAlertDot() {
  try {
    const data = await api('/api/logs?limit=25');
    const items = data.items || [];
    const dot = document.getElementById('logs-alert-dot');
    const hasErr = items.some(l => l.status === 'failed' || String(l.level || '').toUpperCase() === 'ERROR');
    const hasWarn = items.some(l => l.status === 'warning' || String(l.level || '').toUpperCase() === 'WARN');
    dot.className = 'alert-dot' + (hasErr ? ' error' : hasWarn ? ' warn' : '');
  } catch (e) {
    if (e && e.isAuth) return;
  }
}

async function refreshCurrentTab({ prefetchedConfig = null } = {}) {
  try {
    await Promise.all([
      refreshHealthBar(),
      refreshLogAlertDot(),
      loadActiveTabData({ prefetchedConfig })
    ]);
  } catch (e) {
    if (e && e.isAuth) return;
    toast('加载失败：' + e.message, 'error');
  }
}

// ---------- Tab 1: 资讯管理 ----------
function resetNewsFilters() {
  document.getElementById('news-q').value = '';
  document.getElementById('news-status').value = '';
  document.getElementById('news-category').value = '';
  document.getElementById('news-date').value = '';
  loadNews(true);
}

function changeNewsPage(delta) {
  const st = pagination.news;
  const nextOffset = st.offset + delta * st.limit;
  if (nextOffset < 0 || (delta > 0 && nextOffset >= st.total)) return;
  st.offset = nextOffset;
  loadNews(false).catch(e => {
    if (!e || !e.isAuth) toast('加载资讯失败：' + e.message, 'error');
  });
}

async function loadNews(resetPage = false) {
  const st = pagination.news;
  if (resetPage) st.offset = 0;

  const q = document.getElementById('news-q').value.trim();
  const status = document.getElementById('news-status').value;
  const category = document.getElementById('news-category').value;
  const date = document.getElementById('news-date').value;

  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (status) params.set('status', status);
  if (category) params.set('category', category);
  if (date) params.set('date', date);
  params.set('limit', String(st.limit));
  params.set('offset', String(st.offset));

  const data = await api(`/api/news?${params.toString()}`);
  const items = data.items || [];
  const total = Number(data.total ?? data.count ?? items.length);

  if (items.length === 0 && total > 0 && st.offset > 0) {
    st.offset = Math.max(0, Math.floor((total - 1) / st.limit) * st.limit);
    return loadNews(false);
  }

  st.total = total;
  document.getElementById('news-count-badge').textContent = `${total} 条`;
  updatePaginationUI('news', st);

  const tb = document.getElementById('news-tbody');
  if (!items.length) {
    tb.innerHTML = '<tr><td colspan="6" class="empty">暂无符合条件的资讯数据</td></tr>';
    return;
  }

  tb.innerHTML = items.map(n => {
    const isIgnored = n.status === 'ignored';
    const toggleStatus = isIgnored ? 'candidate' : 'ignored';
    const toggleLabel = isIgnored ? '恢复待选' : '忽略';
    return `
      <tr>
        <td class="nowrap num">${esc(n.date)}</td>
        <td class="title-cell">
          <a href="${esc(safeUrl(n.url))}" target="_blank" rel="noopener noreferrer"><strong>${esc(n.title)}</strong></a>
          ${n.summary ? `<div class="sub">${esc(String(n.summary).slice(0, 160))}</div>` : ''}
        </td>
        <td class="nowrap">
          <div style="font-weight:500;margin-bottom:4px;">${esc(n.source_name || '-')}</div>
          ${categoryBadge(n.category)}
        </td>
        <td class="num"><strong>${esc(n.score)}</strong></td>
        <td>${statusBadge(n.status)}</td>
        <td class="nowrap">
          <div class="row-actions">
            <button type="button" class="btn btn-sm btn-primary" data-id="${esc(n.id)}" title="自动翻译为中文并推送至微信草稿箱" onclick="publishNewsItem(this.dataset.id, this)">一键翻译推送</button>
            <button type="button" class="btn btn-sm" data-id="${esc(n.id)}" data-next="${esc(toggleStatus)}" onclick="setNewsStatus(this.dataset.id, this.dataset.next)">${toggleLabel}</button>
            <button type="button" class="btn btn-sm btn-danger-ghost" data-id="${esc(n.id)}" onclick="deleteNewsItem(this.dataset.id)">删除</button>
          </div>
        </td>
      </tr>
    `;
  }).join('');
}

async function publishNewsItem(id, btnEl) {
  if (!id) return;
  if (btnEl) {
    if (btnEl.disabled) return;
    btnEl.disabled = true;
    btnEl.textContent = '翻译推送中...';
  }
  try {
    const res = await api(`/api/news/${encodeURIComponent(id)}/publish`, {
      method: 'POST'
    });
    if (res.ok) {
      const titleText = res.title ? `：《${res.title}》` : '';
      toast(`已翻译并推送草稿${titleText}`, 'success');
    } else {
      const errMsg = res.message || res.detail || '翻译推送失败';
      const titlePrefix = (res.title && !String(errMsg).includes(res.title)) ? `《${res.title}》 ` : '';
      toast(`${titlePrefix}${errMsg}`, 'error');
    }
    await refreshWorkspaceAfterChange();
  } catch (e) {
    if (e && e.isAuth) return;
    toast('一键翻译推送失败：' + (e.message || '请求异常'), 'error');
  } finally {
    if (btnEl && btnEl.isConnected) {
      btnEl.disabled = false;
      btnEl.textContent = '一键翻译推送';
    }
  }
}

async function setNewsStatus(id, status) {
  try {
    await api(`/api/news/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ status })
    });
    toast(status === 'ignored' ? '已标记为忽略' : '已恢复为待选', 'success');
    await loadNews(false);
  } catch (e) {
    if (e && e.isAuth) return;
    toast('更新状态失败：' + e.message, 'error');
  }
}

async function deleteNewsItem(id) {
  if (!confirm('确认删除这条资讯记录吗？删除后无法恢复。')) return;
  try {
    await api(`/api/news/${encodeURIComponent(id)}`, { method: 'DELETE' });
    toast('资讯已删除', 'success');
    await loadNews(false);
  } catch (e) {
    if (e && e.isAuth) return;
    toast('删除失败：' + e.message, 'error');
  }
}

// ---------- Tab 2: 文章管理 ----------
function resetArticleFilters() {
  document.getElementById('articles-q').value = '';
  document.getElementById('articles-status').value = '';
  document.getElementById('articles-category').value = '';
  loadArticles(true);
}

function changeArticlesPage(delta) {
  const st = pagination.articles;
  const nextOffset = st.offset + delta * st.limit;
  if (nextOffset < 0 || (delta > 0 && nextOffset >= st.total)) return;
  st.offset = nextOffset;
  loadArticles(false).catch(e => {
    if (!e || !e.isAuth) toast('加载文章失败：' + e.message, 'error');
  });
}

function getSelectedArticleDates() {
  return Array.from(document.querySelectorAll('.article-row-check:checked'))
    .map(el => el.value)
    .filter(Boolean);
}

function syncArticleSelectionUI() {
  const allChecks = Array.from(document.querySelectorAll('.article-row-check'));
  const checkedCount = allChecks.filter(el => el.checked).length;
  const total = allChecks.length;
  const allChecked = total > 0 && checkedCount === total;
  const indeterminate = checkedCount > 0 && checkedCount < total;

  for (const id of ['articles-select-all', 'articles-thead-select-all']) {
    const box = document.getElementById(id);
    if (box) {
      box.checked = allChecked;
      box.indeterminate = indeterminate;
      box.disabled = total === 0;
    }
  }

  const batchBtn = document.getElementById('btn-articles-batch-delete');
  if (batchBtn) {
    batchBtn.disabled = checkedCount === 0;
    batchBtn.textContent = checkedCount > 0 ? `批量清理 (${checkedCount})` : '批量清理';
  }

  const clearAllBtn = document.getElementById('btn-articles-clear-all');
  if (clearAllBtn) {
    clearAllBtn.disabled = total === 0 && pagination.articles.total === 0;
  }
}

function toggleSelectAllArticles(checked) {
  document.querySelectorAll('.article-row-check').forEach(el => {
    el.checked = Boolean(checked);
  });
  syncArticleSelectionUI();
}

async function loadArticles(resetPage = false) {
  const st = pagination.articles;
  if (resetPage) st.offset = 0;

  const q = document.getElementById('articles-q').value.trim();
  const status = document.getElementById('articles-status').value;
  const category = document.getElementById('articles-category').value;

  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (status) params.set('status', status);
  if (category) params.set('category', category);
  params.set('limit', String(st.limit));
  params.set('offset', String(st.offset));

  const data = await api(`/api/articles?${params.toString()}`);
  const items = data.items || [];
  const total = Number(data.total ?? data.count ?? items.length);

  if (items.length === 0 && total > 0 && st.offset > 0) {
    st.offset = Math.max(0, Math.floor((total - 1) / st.limit) * st.limit);
    return loadArticles(false);
  }

  st.total = total;
  document.getElementById('articles-count-badge').textContent = `${total} 篇`;
  updatePaginationUI('articles', st);

  const tb = document.getElementById('articles-tbody');
  if (!items.length) {
    tb.innerHTML = '<tr><td colspan="8" class="empty">暂无符合条件的已入库文章</td></tr>';
    syncArticleSelectionUI();
    return;
  }

  tb.innerHTML = items.map(a => {
    const coverCleared = !a.cover_path;
    const canPush = (a.status === 'draft_failed' || a.status === 'ready');
    const pushBtnLabel = a.status === 'draft_failed' ? '重试推草稿' : '推送草稿';
    const pushBtnTitle = a.status === 'ready' ? '推送文章到微信草稿箱' : '重新推送失败的文章到微信草稿箱';
    return `
      <tr>
        <td class="col-check">
          <input type="checkbox" class="row-check article-row-check" value="${esc(a.date)}" aria-label="选择文章 ${esc(a.date)}" onchange="syncArticleSelectionUI()" />
        </td>
        <td class="nowrap num"><strong>${esc(a.date)}</strong></td>
        <td class="title-cell">
          <strong>${esc(a.title)}</strong>
          ${a.subtitle ? `<div class="sub">${esc(a.subtitle)}</div>` : ''}
        </td>
        <td class="nowrap">
          ${categoryBadge(a.category)}
          ${a.template ? `<div class="muted" style="font-size:11px;margin-top:4px;">${esc(a.template)}</div>` : ''}
        </td>
        <td class="nowrap num">
          <div><strong>${Number(a.word_count || 0)}</strong> 字</div>
          <div class="muted" style="font-size:11px;">${Number(a.sources_count || 0)} 来源</div>
        </td>
        <td class="nowrap">
          ${a.review_passed
            ? '<span class="badge badge-green">审核通过</span>'
            : '<span class="badge badge-yellow">待人工复核</span>'}
        </td>
        <td>
          <div class="status-stack">
            ${statusBadge(a.status)}
            ${coverCleared ? '<span class="badge badge-outline" title="推送草稿成功后已自动清理本地封面图释放磁盘空间">已清图</span>' : ''}
          </div>
        </td>
        <td class="nowrap">
          <div class="row-actions">
            <button type="button" class="btn btn-sm" data-date="${esc(a.date)}" onclick="openArticleModal(this.dataset.date)">查看正文</button>
            ${canPush
              ? `<button type="button" class="btn btn-sm btn-primary" data-date="${esc(a.date)}" title="${esc(pushBtnTitle)}" onclick="pushDraftFromArticles(this.dataset.date, this)">${pushBtnLabel}</button>`
              : ''}
            <button type="button" class="btn btn-sm btn-danger-ghost" data-date="${esc(a.date)}" data-title="${esc(a.title || a.date)}" onclick="deleteArticleItem(this.dataset.date, this.dataset.title, this)">删除</button>
          </div>
        </td>
      </tr>
    `;
  }).join('');
  syncArticleSelectionUI();
}

async function deleteArticleItem(dateStr, title, btnEl) {
  if (!dateStr) return;
  const ok = confirm(
    `确认永久删除文章「${title || dateStr}」（${dateStr}）吗？\n\n此操作将永久删除该文章记录、本地图片与存储缓存，以及关联的发布/草稿记录，且无法恢复。`
  );
  if (!ok) return;
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = '删除中...';
  }
  try {
    const res = await api(`/api/articles/${encodeURIComponent(dateStr)}`, { method: 'DELETE' });
    const freed = res.freed_human || formatBytes(res.freed_bytes || 0);
    const files = Number(res.removed_files || 0);
    toast(`已删除文章，清理文件 ${files} 个，释放空间 ${freed}`, 'success');
    if (modalArticle && modalArticle.date === dateStr) closeArticleModal();
    await refreshWorkspaceAfterChange();
  } catch (e) {
    if (e && e.isAuth) return;
    toast('删除文章失败：' + e.message, 'error');
    if (btnEl && btnEl.isConnected) {
      btnEl.disabled = false;
      btnEl.textContent = '删除';
    }
  }
}

async function batchDeleteArticles() {
  const dates = getSelectedArticleDates();
  if (!dates.length) return;
  const ok = confirm(
    `确认批量清理选中的 ${dates.length} 篇文章吗？\n\n此操作将永久删除所选文章记录、本地图片与存储缓存，以及关联的发布/草稿记录，且无法恢复。`
  );
  if (!ok) return;
  const btn = document.getElementById('btn-articles-batch-delete');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '清理中...';
  }
  try {
    const res = await api('/api/articles/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ dates })
    });
    const count = Number(res.deleted_count ?? dates.length);
    const freed = res.freed_human || formatBytes(res.freed_bytes || 0);
    const files = Number(res.removed_files || 0);
    toast(`已批量清理 ${count} 篇文章，清理文件 ${files} 个，释放空间 ${freed}`, 'success');
    if (modalArticle && dates.includes(modalArticle.date)) closeArticleModal();
    await refreshWorkspaceAfterChange();
  } catch (e) {
    if (e && e.isAuth) return;
    toast('批量清理文章失败：' + e.message, 'error');
    syncArticleSelectionUI();
  }
}

async function clearAllArticles() {
  const ok = confirm(
    '确认清空全部文章与缓存吗？\n\n此操作将永久删除所有文章记录、全部本地图片与存储缓存，以及关联的发布/草稿记录，且无法恢复！'
  );
  if (!ok) return;
  const btn = document.getElementById('btn-articles-clear-all');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '清空中...';
  }
  try {
    const res = await api('/api/articles/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ all: true })
    });
    const count = Number(res.deleted_count ?? 0);
    const freed = res.freed_human || formatBytes(res.freed_bytes || 0);
    const files = Number(res.removed_files || 0);
    toast(`已清空 ${count} 篇文章，清理文件 ${files} 个，释放空间 ${freed}`, 'success');
    closeArticleModal();
    await refreshWorkspaceAfterChange();
  } catch (e) {
    if (e && e.isAuth) return;
    toast('清空文章失败：' + e.message, 'error');
  } finally {
    if (btn) btn.textContent = '清空文章与缓存';
    syncArticleSelectionUI();
  }
}

async function pushDraftFromArticles(dateStr, btnEl) {
  const originalLabel = btnEl ? btnEl.textContent : '推送草稿';
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = '推送中...';
  }
  try {
    const res = await api(`/api/wechat/draft/${encodeURIComponent(dateStr)}`, {
      method: 'POST',
      body: JSON.stringify({ simulate_failure: false })
    });
    toast(res.message || (res.ok ? '已成功推送至公众号草稿箱' : '推送草稿失败'), res.ok ? 'success' : 'error');
    await refreshWorkspaceAfterChange();
  } catch (e) {
    if (e && e.isAuth) return;
    toast('推送草稿失败：' + e.message, 'error');
    if (btnEl && btnEl.isConnected) {
      btnEl.disabled = false;
      btnEl.textContent = originalLabel;
    }
  }
}

// Article Preview Modal
async function openArticleModal(dateStr) {
  try {
    const a = await api(`/api/articles/${encodeURIComponent(dateStr)}`);
    modalArticle = a;
    modalViewMode = 'html';
    document.getElementById('article-modal-title').textContent = `${a.date} · ${a.title}`;
    renderArticleModalBody();
    document.getElementById('article-modal').classList.add('open');
  } catch (e) {
    if (e && e.isAuth) return;
    toast('加载正文失败：' + e.message, 'error');
  }
}

function closeArticleModal() {
  document.getElementById('article-modal').classList.remove('open');
  modalArticle = null;
}

function switchModalView(mode) {
  modalViewMode = mode;
  renderArticleModalBody();
}

function renderArticleModalBody() {
  const a = modalArticle;
  if (!a) return;
  const coverCleared = (!a.cover_path || !a.cover_url);
  const coverSrc = !coverCleared ? `${safeUrl(a.cover_url)}?v=${encodeURIComponent(a.updated_at || '')}` : '';
  const sources = (a.sources && Array.isArray(a.sources.sources)) ? a.sources.sources : [];
  const checks = (a.review && a.review.checks) ? a.review.checks : {};
  const issues = (a.review && Array.isArray(a.review.issues)) ? a.review.issues : [];

  let bodyPanel = '';
  if (modalViewMode === 'md') {
    bodyPanel = `<pre class="md-source">${esc(a.article_md || '暂无 Markdown 原文')}</pre>`;
  } else if (modalViewMode === 'meta') {
    const checkHtml = Object.keys(checks).length
      ? Object.entries(checks).map(([k, v]) => `<div style="margin:4px 0;">${v ? '✅' : '❌'} <span class="mono">${esc(k)}</span></div>`).join('')
      : '<div class="muted">无详细检查项</div>';
    const srcHtml = sources.length
      ? sources.map(s => `
          <div style="margin-bottom:8px;">
            <a href="${esc(safeUrl(s.url || '#'))}" target="_blank" rel="noopener noreferrer">${esc(s.title || s.url || '参考链接')}</a>
            <div class="muted" style="font-size:12px;">${esc(s.source_name || s.publisher || '')} ${esc(s.published_at || '')}</div>
          </div>
        `).join('')
      : '<div class="muted">暂无参考来源</div>';
    bodyPanel = `
      <div class="meta-panel">
        <div class="meta-box">
          <h4>审核结果</h4>
          <div style="margin-bottom:8px;">${a.review_passed ? '<span class="badge badge-green">审核通过</span>' : '<span class="badge badge-yellow">待人工复核</span>'}</div>
          ${checkHtml}
          ${issues.length ? `<div style="margin-top:8px;color:var(--danger);">问题：${esc(JSON.stringify(issues))}</div>` : ''}
        </div>
        <div class="meta-box">
          <h4>参考来源 (${sources.length})</h4>
          ${srcHtml}
        </div>
      </div>
    `;
  } else {
    bodyPanel = `
      ${coverSrc ? `<img class="cover-preview" src="${esc(coverSrc)}" alt="文章封面" onerror="this.style.display='none'" />` : ''}
      <iframe class="preview-iframe" sandbox="" srcdoc="${esc(buildWechatSrcdoc(a.article_html))}"></iframe>
    `;
  }

  document.getElementById('article-modal-body').innerHTML = `
    <div class="preview-meta">
      ${categoryBadge(a.category)}
      ${statusBadge(a.status)}
      ${coverCleared ? '<span class="badge badge-outline">已清图（推送后自动释放空间）</span>' : ''}
      <span>作者：${esc(a.author || DEFAULT_SITE_NAME)}</span>
      <span>字数：${Number(a.word_count || 0)}</span>
      <span>来源：${sources.length}</span>
    </div>
    <div class="sub-tabs">
      <button type="button" class="btn btn-sm sub-tab ${modalViewMode === 'html' ? 'active' : ''}" onclick="switchModalView('html')">公众号排版预览</button>
      <button type="button" class="btn btn-sm sub-tab ${modalViewMode === 'md' ? 'active' : ''}" onclick="switchModalView('md')">Markdown 原文</button>
      <button type="button" class="btn btn-sm sub-tab ${modalViewMode === 'meta' ? 'active' : ''}" onclick="switchModalView('meta')">审核与来源</button>
    </div>
    ${bodyPanel}
  `;
}

async function copyModalHtml() {
  if (!modalArticle || !modalArticle.article_html) {
    return toast('暂无可复制的 HTML 内容', 'error');
  }
  try {
    await navigator.clipboard.writeText(modalArticle.article_html);
    toast('已复制公众号 HTML 到剪贴板', 'success');
  } catch {
    toast('剪贴板受限，请切换至 Markdown 视图手动复制', 'error');
  }
}

// ---------- Tab 3: 发布记录 (+ 条件显示的待重试/失败队列) ----------
function resetPublishFilters() {
  document.getElementById('publish-q').value = '';
  document.getElementById('publish-status').value = '';
  loadPublishTab(true);
}

function changePublishPage(delta) {
  const st = pagination.publish;
  const nextOffset = st.offset + delta * st.limit;
  if (nextOffset < 0 || (delta > 0 && nextOffset >= st.total)) return;
  st.offset = nextOffset;
  loadPublishTab(false).catch(e => {
    if (!e || !e.isAuth) toast('加载发布记录失败：' + e.message, 'error');
  });
}

function getSelectedPublishIds() {
  return Array.from(document.querySelectorAll('.publish-row-check:checked'))
    .map(el => Number(el.value))
    .filter(id => Number.isFinite(id) && id > 0);
}

function syncPublishSelectionUI() {
  const allChecks = Array.from(document.querySelectorAll('.publish-row-check'));
  const checkedCount = allChecks.filter(el => el.checked).length;
  const total = allChecks.length;
  const allChecked = total > 0 && checkedCount === total;
  const indeterminate = checkedCount > 0 && checkedCount < total;

  for (const id of ['publish-select-all', 'publish-thead-select-all']) {
    const box = document.getElementById(id);
    if (box) {
      box.checked = allChecked;
      box.indeterminate = indeterminate;
      box.disabled = total === 0;
    }
  }

  const batchBtn = document.getElementById('btn-publish-batch-delete');
  if (batchBtn) {
    batchBtn.disabled = checkedCount === 0;
    batchBtn.textContent = checkedCount > 0 ? `批量清理 (${checkedCount})` : '批量清理';
  }

  const clearAllBtn = document.getElementById('btn-publish-clear-all');
  if (clearAllBtn) {
    clearAllBtn.disabled = total === 0 && pagination.publish.total === 0;
  }
}

function toggleSelectAllPublish(checked) {
  document.querySelectorAll('.publish-row-check').forEach(el => {
    el.checked = Boolean(checked);
  });
  syncPublishSelectionUI();
}

async function loadPublishTab(resetPage = false) {
  const st = pagination.publish;
  if (resetPage) st.offset = 0;

  const q = document.getElementById('publish-q').value.trim();
  const status = document.getElementById('publish-status').value;

  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (status) params.set('status', status);
  params.set('limit', String(st.limit));
  params.set('offset', String(st.offset));

  const [pubData, draftData] = await Promise.all([
    api(`/api/publish-records?${params.toString()}`),
    api('/api/wechat/drafts')
  ]);

  // Queued / failed retry section (only shown when non-empty)
  const pendingTasks = (draftData.items || []).filter(d => d.status === 'queued' || d.status === 'failed');
  const retryCard = document.getElementById('publish-retry-card');
  const retryBadge = document.getElementById('nav-retry-badge');

  if (pendingTasks.length > 0) {
    retryCard.classList.remove('hidden');
    document.getElementById('retry-count-badge').textContent = `${pendingTasks.length} 条`;
    if (retryBadge) {
      retryBadge.textContent = String(pendingTasks.length);
      retryBadge.classList.remove('hidden');
    }
    document.getElementById('retry-tbody').innerHTML = pendingTasks.map(d => `
      <tr>
        <td class="num">#${Number(d.id)}</td>
        <td class="nowrap num">${esc(d.article_date)}</td>
        <td><strong>${esc(d.title)}</strong></td>
        <td><span class="badge ${d.mode === 'real' ? 'badge-blue' : 'badge-gray'}">${d.mode === 'real' ? '真实' : '模拟'}</span></td>
        <td>${statusBadge(d.status)}</td>
        <td class="num">${Number(d.attempts || 0)} / ${Number(d.max_attempts || 5)}</td>
        <td class="mono nowrap">${esc(d.next_retry_at || '-')}</td>
        <td style="color:var(--danger);font-size:12px;max-width:260px;word-break:break-word;">${esc(d.error_message || '-')}</td>
        <td class="nowrap">
          <button type="button" class="btn btn-sm btn-primary" onclick="retryDraftTask(${Number(d.id)}, this)">立即重试</button>
        </td>
      </tr>
    `).join('');
  } else {
    retryCard.classList.add('hidden');
    if (retryBadge) retryBadge.classList.add('hidden');
  }

  // Publish records table
  const records = pubData.items || [];
  const total = Number(pubData.total ?? pubData.count ?? records.length);

  if (records.length === 0 && total > 0 && st.offset > 0) {
    st.offset = Math.max(0, Math.floor((total - 1) / st.limit) * st.limit);
    return loadPublishTab(false);
  }

  st.total = total;
  document.getElementById('publish-count-badge').textContent = `${total} 条`;
  updatePaginationUI('publish', st);

  const tb = document.getElementById('publish-tbody');
  if (!records.length) {
    tb.innerHTML = '<tr><td colspan="7" class="empty">暂无符合条件的草稿或发布记录</td></tr>';
    syncPublishSelectionUI();
    return;
  }

  tb.innerHTML = records.map(r => `
    <tr>
      <td class="col-check">
        <input type="checkbox" class="row-check publish-row-check" value="${Number(r.id)}" aria-label="选择发布记录 #${Number(r.id)}" onchange="syncPublishSelectionUI()" />
      </td>
      <td class="nowrap num"><strong>${esc(r.article_date)}</strong></td>
      <td><strong>${esc(r.title)}</strong></td>
      <td class="mono">
        ${r.draft_media_id
          ? `<span title="${esc(r.draft_media_id)}">${esc(r.draft_media_id)}</span>`
          : '<span class="muted">-</span>'}
      </td>
      <td class="nowrap">${statusBadge(r.status)}</td>
      <td>
        ${r.published_url
          ? `<div><a href="${esc(safeUrl(r.published_url))}" target="_blank" rel="noopener noreferrer">查看公众号文章</a></div>`
          : ''}
        <div class="muted" style="font-size:12px;">${esc(r.notes || '-')}</div>
      </td>
      <td class="nowrap">
        <div class="row-actions">
          ${r.status !== 'manual_published'
            ? `<button type="button" class="btn btn-sm btn-success" data-id="${Number(r.id)}" data-title="${esc(r.title)}" data-notes="${esc(r.notes || '')}" onclick="openPublishModal(Number(this.dataset.id), this.dataset.title, this.dataset.notes)">标记已人工发布</button>`
            : '<span class="badge badge-green">已完成</span>'}
          <button type="button" class="btn btn-sm btn-danger-ghost" data-id="${Number(r.id)}" data-date="${esc(r.article_date || '')}" data-title="${esc(r.title || '')}" onclick="deletePublishRecordItem(Number(this.dataset.id), this.dataset.date, this.dataset.title, this)">删除</button>
        </div>
      </td>
    </tr>
  `).join('');
  syncPublishSelectionUI();
}

async function deletePublishRecordItem(id, articleDate, title, btnEl) {
  if (!id) return;
  const ok = confirm(
    `确认删除发布记录「${title || articleDate || ('#' + id)}」吗？\n\n注意：删除该发布记录将同步删除「文章管理」中对应的文章记录，以及本地图片与存储缓存，且无法恢复。`
  );
  if (!ok) return;
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = '删除中...';
  }
  try {
    const res = await api(`/api/publish-records/${Number(id)}`, { method: 'DELETE' });
    const delRec = Number(res.deleted_publish_records ?? 1);
    const delArt = Number(res.deleted_articles ?? 0);
    const freed = res.freed_human || formatBytes(res.freed_bytes || 0);
    const files = Number(res.removed_files || 0);
    toast(`已删除 ${delRec} 条发布记录、同步删除 ${delArt} 篇文章（清理文件 ${files} 个，释放 ${freed}）`, 'success');
    if (modalArticle && articleDate && modalArticle.date === articleDate) closeArticleModal();
    await refreshWorkspaceAfterChange();
  } catch (e) {
    if (e && e.isAuth) return;
    toast('删除发布记录失败：' + e.message, 'error');
    if (btnEl && btnEl.isConnected) {
      btnEl.disabled = false;
      btnEl.textContent = '删除';
    }
  }
}

async function batchDeletePublishRecords() {
  const ids = getSelectedPublishIds();
  if (!ids.length) return;
  const ok = confirm(
    `确认批量清理选中的 ${ids.length} 条发布记录吗？\n\n注意：删除发布记录将同步删除「文章管理」中对应的文章记录，以及本地图片与存储缓存，且无法恢复。`
  );
  if (!ok) return;
  const btn = document.getElementById('btn-publish-batch-delete');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '清理中...';
  }
  try {
    const res = await api('/api/publish-records/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ ids })
    });
    const delRec = Number(res.deleted_records ?? ids.length);
    const delArt = Number(res.deleted_articles ?? 0);
    const freed = res.freed_human || formatBytes(res.freed_bytes || 0);
    const files = Number(res.removed_files || 0);
    toast(`已清理 ${delRec} 条发布记录、同步删除 ${delArt} 篇文章（清理文件 ${files} 个，释放 ${freed}）`, 'success');
    await refreshWorkspaceAfterChange();
  } catch (e) {
    if (e && e.isAuth) return;
    toast('批量清理发布记录失败：' + e.message, 'error');
    syncPublishSelectionUI();
  }
}

async function clearAllPublishRecords() {
  const ok = confirm(
    '确认清空全部发布记录与缓存吗？\n\n注意：清空发布记录将同步删除「文章管理」中所有对应的文章记录，以及全部本地图片与存储缓存，且无法恢复！'
  );
  if (!ok) return;
  const btn = document.getElementById('btn-publish-clear-all');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '清空中...';
  }
  try {
    const res = await api('/api/publish-records/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ all: true })
    });
    const delRec = Number(res.deleted_records ?? 0);
    const delArt = Number(res.deleted_articles ?? 0);
    const freed = res.freed_human || formatBytes(res.freed_bytes || 0);
    const files = Number(res.removed_files || 0);
    toast(`已清空 ${delRec} 条发布记录、同步删除 ${delArt} 篇文章（清理文件 ${files} 个，释放 ${freed}）`, 'success');
    closeArticleModal();
    await refreshWorkspaceAfterChange();
  } catch (e) {
    if (e && e.isAuth) return;
    toast('清空发布记录失败：' + e.message, 'error');
  } finally {
    if (btn) btn.textContent = '清空记录与缓存';
    syncPublishSelectionUI();
  }
}

async function retryDraftTask(taskId, btnEl) {
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = '重试中...';
  }
  try {
    const res = await api(`/api/wechat/draft/retry/${Number(taskId)}`, { method: 'POST' });
    toast(res.message || (res.ok ? '草稿重试成功' : '重试仍未成功'), res.ok ? 'success' : 'error');
    await refreshWorkspaceAfterChange();
  } catch (e) {
    if (e && e.isAuth) return;
    toast('重试失败：' + e.message, 'error');
    if (btnEl && btnEl.isConnected) {
      btnEl.disabled = false;
      btnEl.textContent = '立即重试';
    }
  }
}

async function processRetryQueue() {
  try {
    const res = await api('/api/wechat/drafts/process-queue', { method: 'POST' });
    toast(`队列处理完成：共 ${res.processed || 0} 条，成功 ${res.succeeded || 0} 条`, 'success');
    await refreshWorkspaceAfterChange();
  } catch (e) {
    if (e && e.isAuth) return;
    toast('队列处理失败：' + e.message, 'error');
  }
}

// Mark Manual Published Modal
function openPublishModal(recordId, title, existingNotes) {
  pendingPublishRecordId = recordId;
  document.getElementById('publish-modal-subtitle').textContent = `确认文章《${title}》已在微信公众号后台人工审核并发布：`;
  document.getElementById('pub-url-input').value = '';
  document.getElementById('pub-notes-input').value = existingNotes && !existingNotes.includes('待人工')
    ? existingNotes
    : '已在公众号后台人工审核并发布';
  document.getElementById('publish-modal').classList.add('open');
  setTimeout(() => document.getElementById('pub-url-input').focus(), 40);
}

function closePublishModal() {
  document.getElementById('publish-modal').classList.remove('open');
  pendingPublishRecordId = null;
}

document.getElementById('publish-modal-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  if (!pendingPublishRecordId) return;
  const btn = document.getElementById('pub-confirm-btn');
  btn.disabled = true;
  btn.textContent = '提交中...';
  try {
    await api(`/api/publish-records/${Number(pendingPublishRecordId)}`, {
      method: 'PATCH',
      body: JSON.stringify({
        status: 'manual_published',
        published_url: document.getElementById('pub-url-input').value.trim(),
        notes: document.getElementById('pub-notes-input').value.trim()
      })
    });
    closePublishModal();
    toast('已标记为人工发布完成', 'success');
    await refreshWorkspaceAfterChange();
  } catch (e) {
    if (e && e.isAuth) return;
    toast('更新发布记录失败：' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '确认已发布';
  }
});

// ---------- Tab 4: 系统配置 ----------
async function loadSettingsAndBackups(prefetchedConfig = null) {
  const [cfg, bk] = await Promise.all([
    prefetchedConfig ? Promise.resolve(prefetchedConfig) : api('/api/config'),
    api('/api/backups')
  ]);

  const hasSecret = Boolean(cfg.has_wechat_appsecret);
  const maskedSecret = cfg.wechat_appsecret_masked || '****';
  const configured = Boolean(cfg.wechat_configured ?? (cfg.wechat_appid && hasSecret));
  authState.wechat_configured = configured;
  const wxBadge = document.getElementById('cfg-wx-status-badge');
  wxBadge.textContent = configured ? '微信推送已配置' : '尚未配置推送凭证';
  wxBadge.className = 'badge ' + (configured ? 'badge-green' : 'badge-yellow');

  document.getElementById('cfg-appid').value = cfg.wechat_appid || '';
  document.getElementById('cfg-secret').value = '';
  setPasswordVisibility('cfg-secret', document.getElementById('cfg-secret-toggle'), false);
  document.getElementById('cfg-secret').placeholder = hasSecret
    ? `已配置 (${maskedSecret})，留空则保持不变`
    : '请输入 AppSecret';
  document.getElementById('cfg-secret-status').textContent = hasSecret
    ? `已配置 ${maskedSecret}`
    : '未配置';

  document.getElementById('cfg-mock-mode').value = String(Boolean(cfg.wechat_mock_mode));
  document.getElementById('cfg-draft-retry').value = String(Boolean(cfg.draft_retry_enabled));
  document.getElementById('cfg-retry-max').value = Number(cfg.draft_retry_max_attempts || 5);
  document.getElementById('cfg-data-retain-days').value = Number(cfg.data_retain_days || 7);
  document.getElementById('cfg-retain').value = Number(cfg.backup_retain_count || 7);
  document.getElementById('cfg-site-name').value = cfg.site_name || DEFAULT_SITE_NAME;

  applySiteBranding(cfg.site_name);

  const diskHuman = cfg.disk_usage_human || (cfg.disk_usage_bytes != null ? formatBytes(cfg.disk_usage_bytes) : '--');
  document.getElementById('rt-disk').textContent = diskHuman;
  const sql = cfg.sqlite || {};
  document.getElementById('rt-sqlite').textContent = `${String(sql.journal_mode || 'wal').toUpperCase()} (${Number(sql.busy_timeout_ms || 5000)}ms)`;
  document.getElementById('rt-queued').textContent = `${Number(cfg.queued_drafts || 0)} 条`;
  document.getElementById('rt-backups').textContent = `${Number(cfg.backups_count || 0)} 份`;

  const backupDirEl = document.getElementById('backup-dir-path');
  if (backupDirEl) {
    const baseDir = String(cfg.data_dir || '').replace(/[\\/]+$/, '');
    backupDirEl.textContent = baseDir ? `${baseDir}/backups` : 'backups';
  }

  const retainCount = Number(bk.retain_count || cfg.backup_retain_count || 7);
  document.getElementById('backup-retain-badge').textContent = `保留最新 ${retainCount} 份 · 当前 ${Number(bk.count || 0)} 份`;

  const tb = document.getElementById('backups-tbody');
  const items = bk.items || [];
  if (!items.length) {
    tb.innerHTML = '<tr><td colspan="4" class="empty">暂无备份文件，点击上方“一键备份数据库”即可创建</td></tr>';
    return;
  }
  tb.innerHTML = items.map(b => `
    <tr>
      <td class="mono">${esc(b.filename)}</td>
      <td>${b.trigger === 'scheduled' ? '<span class="badge badge-gray">每日自动</span>' : '<span class="badge badge-blue">手动触发</span>'}</td>
      <td class="num">${formatBytes(b.size_bytes)}</td>
      <td class="nowrap num">${esc(b.created_at)}</td>
    </tr>
  `).join('');
}

async function saveSettings() {
  const btn = document.getElementById('btn-save-settings');
  btn.disabled = true;
  btn.textContent = '保存中...';
  try {
    const retainDays = Math.min(365, Math.max(1, Number(document.getElementById('cfg-data-retain-days').value || 7)));
    const backupRetain = Math.min(365, Math.max(3, Number(document.getElementById('cfg-retain').value || 7)));
    const retryMax = Math.min(20, Math.max(1, Number(document.getElementById('cfg-retry-max').value || 5)));

    const payload = {
      site_name: document.getElementById('cfg-site-name').value.trim(),
      wechat_appid: document.getElementById('cfg-appid').value.trim(),
      wechat_mock_mode: document.getElementById('cfg-mock-mode').value === 'true',
      draft_retry_enabled: document.getElementById('cfg-draft-retry').value === 'true',
      draft_retry_max_attempts: retryMax,
      data_retain_days: retainDays,
      backup_retain_count: backupRetain
    };
    const secret = document.getElementById('cfg-secret').value.trim();
    if (secret) payload.wechat_appsecret = secret;

    const savedCfg = await api('/api/config', { method: 'PUT', body: JSON.stringify(payload) });
    toast('系统配置已保存', 'success');
    await Promise.all([
      refreshHealthBar(),
      refreshLogAlertDot(),
      loadSettingsAndBackups(savedCfg)
    ]);
  } catch (e) {
    if (e && e.isAuth) return;
    toast('保存配置失败：' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '保存配置';
  }
}

async function runCleanupNow() {
  const btn = document.getElementById('btn-cleanup-now');
  btn.disabled = true;
  btn.textContent = '清理中...';
  try {
    const [pushedRes, oldRes] = await Promise.all([
      api('/api/cleanup/pushed-media', { method: 'POST' }),
      api('/api/cleanup/run', { method: 'POST' })
    ]);
    const freed = Number(pushedRes.freed_bytes || 0) + Number(oldRes.freed_bytes || 0);
    const removedFiles = Number(pushedRes.removed_files || 0);
    const removedDirs = (oldRes.removed_incoming?.length || 0) + (oldRes.removed_articles?.length || 0);
    toast(`清理完成：已清图片 ${removedFiles} 个，过期目录 ${removedDirs} 个，释放 ${formatBytes(freed)}`, 'success');
    await refreshWorkspaceAfterChange();
  } catch (e) {
    if (e && e.isAuth) return;
    toast('空间清理失败：' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '立即清理过期/已推图片';
  }
}

async function runBackupNow() {
  const btn = document.getElementById('btn-backup-now');
  btn.disabled = true;
  btn.textContent = '备份中...';
  try {
    const res = await api('/api/backups/run', { method: 'POST' });
    toast(`备份成功：${res.filename} (${formatBytes(res.size_bytes)})`, 'success');
    await refreshWorkspaceAfterChange();
  } catch (e) {
    if (e && e.isAuth) return;
    toast('备份失败：' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '一键备份数据库';
  }
}

// ---------- Slide-over Task Log Drawer ----------
function openLogsDrawer() {
  document.getElementById('logs-drawer-backdrop').classList.add('open');
  document.getElementById('logs-drawer').classList.add('open');
  loadLogs(true);
}

function closeLogsDrawer() {
  document.getElementById('logs-drawer-backdrop').classList.remove('open');
  document.getElementById('logs-drawer').classList.remove('open');
}

function resetLogFilters() {
  document.getElementById('log-q').value = '';
  document.getElementById('log-stage').value = '';
  document.getElementById('log-status').value = '';
  document.getElementById('log-date').value = '';
  loadLogs(true);
}

function changeLogsPage(delta) {
  const st = pagination.logs;
  const nextOffset = st.offset + delta * st.limit;
  if (nextOffset < 0 || (delta > 0 && nextOffset >= st.total)) return;
  st.offset = nextOffset;
  loadLogs(false);
}

async function loadLogs(resetPage = false) {
  const st = pagination.logs;
  if (resetPage) st.offset = 0;

  const body = document.getElementById('logs-drawer-body');
  const q = document.getElementById('log-q').value.trim();
  const stage = document.getElementById('log-stage').value;
  const status = document.getElementById('log-status').value;
  const date = document.getElementById('log-date').value;

  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (stage) params.set('stage', stage);
  if (status) params.set('status', status);
  if (date) params.set('date', date);
  params.set('limit', String(st.limit));
  params.set('offset', String(st.offset));

  try {
    const data = await api('/api/logs?' + params.toString());
    const items = data.items || [];
    const total = Number(data.total ?? data.count ?? items.length);

    if (items.length === 0 && total > 0 && st.offset > 0) {
      st.offset = Math.max(0, Math.floor((total - 1) / st.limit) * st.limit);
      return loadLogs(false);
    }

    st.total = total;
    document.getElementById('drawer-logs-count').textContent = `${total} 条`;
    updatePaginationUI('logs', st);

    if (!items.length) {
      body.innerHTML = '<div class="empty">暂无符合条件的任务日志</div>';
      return;
    }

    body.innerHTML = items.map(l => {
      const lvl = String(l.level || 'INFO').toUpperCase();
      const cls = (l.status === 'failed' || lvl === 'ERROR')
        ? 'log-error'
        : (l.status === 'warning' || lvl === 'WARN')
          ? 'log-warn'
          : '';
      const stageLabel = STAGE_LABELS[l.stage] || l.stage || '-';
      return `
        <div class="log-item ${cls}">
          <div class="log-item-head">
            <div class="log-item-meta">
              <span class="badge badge-gray">${esc(stageLabel)}</span>
              ${statusBadge(l.status)}
              ${l.date ? `<span class="mono muted">${esc(l.date)}</span>` : ''}
            </div>
            <span class="mono muted">${esc(l.created_at || '')}</span>
          </div>
          <div class="log-item-msg">${esc(l.message || '')}</div>
        </div>
      `;
    }).join('');
    body.scrollTop = 0;
  } catch (e) {
    if (e && e.isAuth) return;
    body.innerHTML = `<div class="empty" style="color:var(--danger);">加载日志失败：${esc(e.message)}</div>`;
  }
}

// ---------- Global Keyboard Shortcuts ----------
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    closeArticleModal();
    closePublishModal();
    closeLogsDrawer();
    closeMobileSidebar();
  }
});

// ---------- Boot ----------
(async function boot() {
  showBoot();
  try {
    const me = await api('/api/auth/me', { skipAuthRedirect: true });
    authState = {
      authenticated: Boolean(me.authenticated),
      auth_enabled: me.auth_enabled !== false,
      username: me.username || 'admin',
      wechat_configured: Boolean(me.wechat_configured)
    };
    if (!authState.authenticated) {
      showLoginScreen('');
      return;
    }
    if (!authState.wechat_configured) {
      showOnboardingScreen();
      return;
    }
    await enterMainApp();
  } catch (e) {
    showLoginScreen('无法连接认证接口：' + (e.message || '请稍后重试'));
  }
})();
