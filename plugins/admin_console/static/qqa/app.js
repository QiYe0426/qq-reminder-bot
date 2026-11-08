// 猎宝 · 调试台前端（借鉴 QQ Agent 控制台的视觉与信息结构）
// 只做只读展示 + 人设保存；所有请求都走 /hunterbot/admin-console/qqa。
'use strict';

const PREFIX = '/hunterbot/admin-console/qqa';
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  overview: null,
  runs: [],
  selectedId: null,
  detail: null,
  tab: 'runs',
  sseOk: false,
};

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function fmtTime(ts) {
  if (!ts) return '-';
  const d = new Date(Number(ts) * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtNum(n) {
  return Number(n || 0).toLocaleString('zh-CN');
}

async function api(path, options) {
  const res = await fetch(PREFIX + path, { credentials: 'same-origin', ...(options || {}) });
  if (!res.ok) throw new Error(path + ' -> ' + res.status);
  return res.json();
}

// ── 主题 ──────────────────────────────────────────
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme === 'light' ? 'light' : 'dark');
  try { localStorage.setItem('qqa-theme', theme); } catch (e) { /* ignore */ }
}

// ── 页签 ──────────────────────────────────────────
function switchTab(tab) {
  state.tab = tab;
  $$('#tabs .tab').forEach((btn) => btn.classList.toggle('active', btn.dataset.tab === tab));
  $$('.view').forEach((view) => view.classList.toggle('active', view.id === 'view-' + tab));
  if (tab === 'usage') renderUsage();
  if (tab === 'settings') renderSettings();
  if (tab === 'connection') renderConnection();
}

// ── 顶部状态 ───────────────────────────────────────
function renderTopbar() {
  const o = state.overview;
  if (!o) return;
  const connected = !!(o.bot && o.bot.connected);
  const dot = $('#onebot-dot');
  dot.className = 'dot ' + (connected ? 'dot-on' : 'dot-off');
  $('#onebot-label').textContent = connected ? 'OneBot 已连接' : 'OneBot 未连接';
  const models = o.models || {};
  $('#model-label').textContent = '模型：' + (models.agent || models.chat || '-');
  const today = o.today || {};
  $('#usage-label').textContent = `今日：${fmtNum(today.runs)} 次 / ${fmtNum(
    (today.prompt_tokens || 0) + (today.completion_tokens || 0)
  )} tokens`;
  const err = $('#error-label');
  err.textContent = '失败：' + fmtNum(today.errors);
  err.style.color = today.errors ? 'var(--err)' : '';
}

// ── 会话列表 ───────────────────────────────────────
function statusBadge(status) {
  if (status === 'error') return '<span class="badge err">失败</span>';
  if (status === 'running') return '<span class="badge warn">运行中</span>';
  return '<span class="badge ok">成功</span>';
}

function renderRunList() {
  const box = $('#run-items');
  $('#run-count').textContent = `会话 ${state.runs.length} 条`;
  if (!state.runs.length) {
    box.innerHTML = '<div class="empty-hint" style="padding:18px">还没有任何 AI 对话记录。<br>等群里有人 @ 猎宝后，这里会出现完整过程。</div>';
    return;
  }
  box.innerHTML = state.runs
    .map((run) => {
      const active = run.run_id === state.selectedId ? ' active' : '';
      const tokens = (run.usage_prompt_tokens || 0) + (run.usage_completion_tokens || 0);
      return `<div class="session-item${active}" data-run="${esc(run.run_id)}">
        <div class="session-title">${esc(run.trigger_summary || '(无触发摘要)')}</div>
        <div class="session-meta">
          <span>${esc(run.chat_key || '-')}</span>
          <span>${fmtTime(run.started_at)}</span>
        </div>
        <div class="session-meta">
          ${statusBadge(run.status)}
          <span>${fmtNum(tokens)} tok</span>
          <span>${fmtNum(run.step_count)} 步</span>
        </div>
      </div>`;
    })
    .join('');
  $$('#run-items .session-item').forEach((el) => {
    el.addEventListener('click', () => selectRun(el.dataset.run));
  });
}

// ── 会话详情 ───────────────────────────────────────
function stepCard(step) {
  const kind = step.kind;
  if (kind === 'think') {
    return `<details class="collapsible tool-flow think-flow" open>
      <summary><span class="tool-ico">💭</span><span class="tool-name-flow">思考</span>
      <span class="tool-label">不发送</span></summary>
      <div class="coll-body">${esc(step.result || '')}</div></details>`;
  }
  if (kind === 'tool_call') {
    const ok = step.ok !== false;
    return `<details class="collapsible tool-flow ${ok ? '' : 'tool-error'}" open>
      <summary><span class="tool-ico">⚒</span><span class="tool-name-flow">${esc(step.tool_name || 'tool')}</span>
      <span class="tool-label">${step.duration_ms != null ? step.duration_ms + 'ms' : ''}</span>
      <span class="badge ${ok ? 'ok' : 'err'}">${ok ? '成功' : '失败'}</span></summary>
      <div class="coll-body">
        <div class="tool-args">${esc(JSON.stringify(step.args, null, 1))}</div>
        <div class="tool-result ${ok ? '' : 'is-error'}">${esc(step.result || '')}</div>
      </div></details>`;
  }
  if (kind === 'sent') {
    return `<details class="collapsible tool-flow sent-flow" open>
      <summary><span class="tool-ico">💬</span><span class="tool-name-flow">已发送到 QQ</span>
      <span class="badge ok">已送出</span></summary>
      <div class="coll-body sent-body">${esc(step.result || '')}</div></details>`;
  }
  if (kind === 'retrieval') {
    return `<details class="collapsible tool-flow" open>
      <summary><span class="tool-ico">🔎</span><span class="tool-name-flow">检索</span></summary>
      <div class="coll-body">${esc(JSON.stringify({ args: step.args, result: step.result }, null, 1))}</div></details>`;
  }
  return `<details class="collapsible tool-flow">
    <summary><span class="tool-ico">•</span><span class="tool-name-flow">${esc(kind)}</span></summary>
    <div class="coll-body">${esc(step.result || '')}</div></details>`;
}

function renderRunDetail() {
  const box = $('#run-detail');
  const run = state.detail;
  if (!run) {
    box.innerHTML = '<div class="empty-hint">← 选择左侧会话查看完整过程</div>';
    return;
  }
  const tokens = (run.usage_prompt_tokens || 0) + (run.usage_completion_tokens || 0);
  const html = [];
  html.push(`<div class="detail-header">
    <div>
      <div class="detail-title">${esc(run.trigger_summary || '(无摘要)')}</div>
      <div class="session-meta">
        <span>${esc(run.chat_key)}</span>
        <span>${fmtTime(run.started_at)} → ${fmtTime(run.finished_at)}</span>
        ${statusBadge(run.status)}
      </div>
    </div>
    <div class="detail-stat">${fmtNum(tokens)} tokens</div>
  </div>`);

  if (run.system_prompt) {
    html.push(`<details class="collapsible"><summary>系统提示（${run.system_prompt.length} 字符）</summary>
      <div class="coll-body">${esc(run.system_prompt)}</div></details>`);
  }
  if (run.user_prompt) {
    html.push(`<details class="collapsible"><summary>本次输入（${run.user_prompt.length} 字符）</summary>
      <div class="coll-body">${esc(run.user_prompt)}</div></details>`);
  }
  html.push('<div class="msg-flow">');
  (run.steps || []).forEach((step) => html.push(stepCard(step)));
  html.push('</div>');

  if (run.error) {
    html.push(`<details class="collapsible tool-flow error-flow" open>
      <summary><span class="tool-ico">⚠</span><span class="tool-name-flow">错误</span></summary>
      <div class="coll-body error-body">${esc(run.error)}</div></details>`);
  }
  html.push(`<div class="session-meta" style="margin-top:10px">
    <span>模型：${esc(run.model || '-')}</span>
    <span>prompt ${fmtNum(run.usage_prompt_tokens)}</span>
    <span>completion ${fmtNum(run.usage_completion_tokens)}</span>
    <span>run_id：${esc(run.run_id)}</span>
  </div>`);
  box.innerHTML = html.join('');
}

async function selectRun(runId) {
  state.selectedId = runId;
  renderRunList();
  const box = $('#run-detail');
  box.innerHTML = '<div class="empty-hint">加载中…</div>';
  try {
    state.detail = await api('/api/runs/' + encodeURIComponent(runId));
  } catch (e) {
    state.detail = null;
    box.innerHTML = '<div class="empty-hint">加载失败：' + esc(e.message) + '</div>';
    return;
  }
  renderRunDetail();
}

// ── 用量页 ────────────────────────────────────────
function renderUsage() {
  const page = $('#usage-page');
  const o = state.overview || {};
  const today = o.today || {};
  const stats = o.stats || {};
  const total = (today.prompt_tokens || 0) + (today.completion_tokens || 0);
  page.innerHTML = `<div class="usage-wrap">
    <div class="usage-head"><h2>用量（只统计 AI 对话，价格核算在 P3）</h2></div>
    <div class="usage-cards">
      <div class="usage-card accent">
        <div class="uc-label">今日 tokens</div>
        <div class="uc-value">${fmtNum(total)}</div>
        <div class="uc-sub">prompt ${fmtNum(today.prompt_tokens)} / completion ${fmtNum(today.completion_tokens)}</div>
      </div>
      <div class="usage-card">
        <div class="uc-label">今日对话</div>
        <div class="uc-value">${fmtNum(today.runs)}</div>
        <div class="uc-sub">失败 ${fmtNum(today.errors)}</div>
      </div>
      <div class="usage-card">
        <div class="uc-label">累计对话</div>
        <div class="uc-value">${fmtNum(stats.total)}</div>
        <div class="uc-sub">累计失败 ${fmtNum(stats.errors)}</div>
      </div>
    </div>
    <div class="usage-h3" style="margin-top:18px">最近会话</div>
    <table class="usage-table">
      <thead><tr><th>时间</th><th>会话</th><th>触发</th><th>tokens</th><th>状态</th></tr></thead>
      <tbody>
        ${(state.runs || [])
          .map(
            (r) => `<tr><td>${fmtTime(r.started_at)}</td><td>${esc(r.chat_key)}</td>
              <td>${esc(r.trigger_summary || '')}</td>
              <td>${fmtNum((r.usage_prompt_tokens || 0) + (r.usage_completion_tokens || 0))}</td>
              <td>${esc(r.status)}</td></tr>`
          )
          .join('') || '<tr><td colspan="5" class="muted">暂无数据</td></tr>'}
      </tbody>
    </table>
  </div>`;
}

// ── 设置页 ────────────────────────────────────────
async function renderSettings() {
  const page = $('#settings-page');
  page.innerHTML = `<div class="usage-wrap"><div class="usage-h3">人设</div>
    <div class="muted" style="margin:6px 0 10px">保存后写入 data/bot_persona_prompt.txt，AI 对话实时读取。</div>
    <textarea id="persona-text" style="width:100%;min-height:320px;padding:10px;border-radius:8px;
      background:var(--bg1);color:var(--tx);border:1px solid var(--bd);font-family:var(--font-mono)"></textarea>
    <div style="margin-top:10px;display:flex;gap:8px;align-items:center">
      <button class="btn btn-primary" id="persona-save">保存</button>
      <span id="persona-status" class="muted"></span>
    </div></div>`;
  try {
    const data = await api('/api/state');
    $('#persona-text').value = data.persona || '';
  } catch (e) {
    $('#persona-status').textContent = '加载失败：' + e.message;
  }
  $('#persona-save').addEventListener('click', async () => {
    const status = $('#persona-status');
    status.textContent = '保存中…';
    try {
      await api('/api/persona', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ persona: $('#persona-text').value }),
      });
      status.textContent = '已保存';
    } catch (e) {
      status.textContent = '保存失败：' + e.message;
    }
  });
}

// ── 连接页 ────────────────────────────────────────
function renderConnection() {
  const page = $('#connection-page');
  const o = state.overview || {};
  const bot = o.bot || {};
  const models = o.models || {};
  page.innerHTML = `<div class="usage-wrap"><div class="usage-h3">连接与运行环境</div>
    <table class="usage-table" style="margin-top:10px">
      <tbody>
        <tr><th style="width:180px">OneBot</th><td>${bot.connected ? '<span class="badge ok">已连接</span>' : '<span class="badge err">未连接</span>'}
          　QQ 号：${esc(bot.self_id || '-')}</td></tr>
        <tr><th>版本</th><td>${esc(o.version || '-')}</td></tr>
        <tr><th>服务器时间</th><td>${esc(o.server_time || '-')}</td></tr>
        <tr><th>对话模型</th><td>${esc(models.chat || '-')}</td></tr>
        <tr><th>Agent 模型</th><td>${esc(models.agent || '-')}</td></tr>
        <tr><th>总结模型</th><td>${esc(models.summary || '-')}</td></tr>
        <tr><th>SSE 实时通道</th><td>${state.sseOk ? '<span class="badge ok">正常</span>' : '<span class="badge warn">降级为轮询</span>'}</td></tr>
      </tbody>
    </table>
    <div class="muted" style="margin-top:14px">
      旧控制台（人设之外的管理功能）：<a href="/hunterbot/admin-console">打开</a>　
      下一步 P1 会把存档/记忆/检索/调度/产物/群管理等页面逐步搬到这里。
    </div></div>`;
}

// ── 数据刷新 ───────────────────────────────────────
function applySnapshot(payload) {
  if (!payload) return;
  if (payload.overview) state.overview = payload.overview;
  if (Array.isArray(payload.runs)) state.runs = payload.runs;
  renderTopbar();
  renderRunList();
  if (state.tab === 'usage') renderUsage();
  if (state.tab === 'connection') renderConnection();
}

async function refreshOnce() {
  try {
    const [overview, runs] = await Promise.all([
      api('/api/overview'),
      api('/api/runs?limit=50'),
    ]);
    state.sseOk = false;
    applySnapshot({ overview, runs: runs.runs });
  } catch (e) {
    // 未登录/会话过期时静默失败，页面顶部会显示未连接
  }
}

function connectEvents() {
  if (!window.EventSource) return;
  let es;
  try {
    es = new EventSource(PREFIX + '/api/events');
  } catch (e) {
    return;
  }
  es.onopen = () => {
    state.sseOk = true;
    if (state.tab === 'connection') renderConnection();
  };
  es.onmessage = (event) => {
    try {
      applySnapshot(JSON.parse(event.data));
    } catch (e) { /* ignore */ }
  };
  es.onerror = () => {
    state.sseOk = false;
    es.close();
    setTimeout(connectEvents, 5000);
  };
}

// ── 启动 ──────────────────────────────────────────
function init() {
  applyTheme(localStorage.getItem('qqa-theme') || 'dark');
  $$('#tabs .tab').forEach((btn) => btn.addEventListener('click', () => switchTab(btn.dataset.tab)));
  $('#theme-btn').addEventListener('click', () => {
    const now = document.documentElement.getAttribute('data-theme');
    applyTheme(now === 'light' ? 'dark' : 'light');
  });
  refreshOnce();
  connectEvents();
  setInterval(() => {
    if (!state.sseOk) refreshOnce();
  }, 5000);
}

document.addEventListener('DOMContentLoaded', init);
