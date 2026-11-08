// 猎宝 · 调试台（v2：会话 + 存档 + 群管理 + 画像 + 知识库 + 用量 + 设置 + 连接）
// 视觉沿用 QQ Agent 控制台样式；旧控制台 API 直接复用（同源 Cookie 鉴权）。
'use strict';

const PREFIX = '/hunterbot/admin-console/qqa';
const LEGACY = '/hunterbot/admin-console';
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const FEATURE_TREE = [
  {
    key: 'ai_chat', label: 'AI 对话', desc: '群友 @ 或叫名字时允许 AI 回复',
    children: [{ tier: true }],
  },
  { key: 'constant_retort', label: '固定回怼', desc: '命中关键词时发送固定内容（确定性，不进 LLM）' },
  { key: 'keyword_retort', label: '关键词回怼', desc: '按控制台维护的关键词规则回复（确定性，不进 LLM）' },
  { key: 'bot_tease', label: '调戏其他 bot', desc: '其他 bot 发言后的限流短回复' },
  {
    key: 'collector', label: '消息采集', desc: '把群消息写入归档；日报与画像的基础',
    children: [
      {
        key: 'daily_report', label: '日报', desc: '允许生成这个群的日报',
        children: [{ key: 'daily_report_auto', label: '自动日报', desc: '每天 04:00 自动发送' }],
      },
      { key: 'companion', label: '陪伴画像', desc: '为该群群友记录长期画像' },
    ],
  },
  { key: 'hourly_chime', label: '🎒 常数报时', desc: '每小时 58 分报时' },
];

// 等级 → 缩进样式
function treeIndent(depth) {
  return `padding-left:${12 + depth * 22}px`;
}

function featureTreeHtml(nodes, features, depth, parentOn) {
  return nodes.map((node) => {
    if (node.tier) {
      return tierBlockHtml(depth + 1, parentOn);
    }
    const on = !!features[node.key];
    const disabled = depth > 0 && !parentOn;
    const hint = disabled ? '<span class="tag plain">需先开启上一级</span>' : '';
    const row = `<label class="switch-row tree-node ${on ? 'on' : ''}" style="${treeIndent(depth)}">
      <span class="switch-text"><span class="switch-label">${esc(node.label)} ${hint}</span>
        <span class="switch-desc">${esc(node.desc)}</span></span>
      <input type="checkbox" ${on ? 'checked' : ''} ${disabled ? 'disabled' : ''} data-feature="${node.key}" />
      <span class="switch-track"><span class="switch-thumb"></span></span>
    </label>`;
    const children = node.children && on ? featureTreeHtml(node.children, features, depth + 1, true) : '';
    return row + children;
  }).join('');
}

function tierBlockHtml(depth, parentOn) {
  const cfg = state.tierConfig || {};
  const runtime = (state.overview || {}).runtime || {};
  const active = !!(runtime.enabled && (runtime.groups || []).map(String).includes(String(state.groupId)));
  const note = active
    ? '已生效：未 @ 时按下方规则决定是否接话。'
    : '未生效：需要在服务器 .env.local 打开 AGENT_RUNTIME_ROUTING=1 并把本群加入 AGENT_RUNTIME_GROUP_IDS。';
  return `<div class="tier-block" style="${treeIndent(depth)}">
    <label class="switch-row tree-node ${cfg.enabled ? 'on' : ''}" style="padding-left:0">
      <span class="switch-text"><span class="switch-label">触发档位（关键词 / 概率插话）</span>
        <span class="switch-desc">@ 和叫名字永远触发；这里控制“没被叫到时”是否可能接话。默认关闭。</span></span>
      <input type="checkbox" ${cfg.enabled ? 'checked' : ''} ${parentOn ? '' : 'disabled'} data-tier="enabled" />
      <span class="switch-track"><span class="switch-thumb"></span></span>
    </label>
    <div class="tier-grid">
      <label class="field"><span>关键词（逗号分隔）</span>
        <input class="text-input" id="tier-keywords" value="${esc(cfg.keywords || '')}" placeholder="例如：猎宝,小鲸鱼" ${parentOn ? '' : 'disabled'} /></label>
      <label class="field"><span>概率插话（%）</span>
        <input class="num-input" type="number" min="0" max="100" step="1" id="tier-probability" value="${esc(cfg.probability ?? 5)}" ${parentOn ? '' : 'disabled'} /></label>
      <label class="field"><span>冷却（分钟）</span>
        <input class="num-input" type="number" min="0" id="tier-cooldown" value="${esc(cfg.cooldown_minutes ?? 10)}" ${parentOn ? '' : 'disabled'} /></label>
      <label class="field"><span>每日上限（次）</span>
        <input class="num-input" type="number" min="0" id="tier-daily" value="${esc(cfg.daily_limit ?? 60)}" ${parentOn ? '' : 'disabled'} /></label>
    </div>
    <div class="tier-note ${active ? 'ok' : 'warn'}">${esc(note)}</div>
    <div class="tier-note">参考成本：按 3,500 条/天、5% 概率约 175 次/天；缓存友好时约 ¥2.5–5/周，日上限会兜底。</div>
  </div>`;
}

const LIMIT_FEATURES = [
  ['bot_tease', '调戏其他 bot'],
  ['constant_retort', '固定回怼'],
  ['keyword_retort', '关键词回怼'],
];

const state = {
  overview: null,
  runs: [],
  selectedId: null,
  detail: null,
  tab: 'runs',
  sseOk: false,
  groups: [],
  groupId: '',
  groupDetail: null,
  members: [],
  memberQuery: '',
  memberSelected: '',
  companion: null,
  archive: { messages: [], offset: 0, hasMore: false, query: '' },
  knowledge: [],
  knowledgeKind: 'card',
  knowledgeKinds: [],
  knowledgeQuery: '',
  knowledgeSelected: null,
  tierConfig: null,
};

// ── 基础工具 ─────────────────────────────────────────
function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
function fmtNum(n) { return Number(n || 0).toLocaleString('zh-CN'); }
function fmtTime(ts) {
  if (!ts) return '-';
  if (typeof ts === 'string' && ts.includes('-')) return ts.slice(5, 16);
  const d = new Date(Number(ts) * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

async function request(url, options) {
  const res = await fetch(url, { credentials: 'same-origin', ...(options || {}) });
  if (!res.ok) throw new Error(url + ' -> ' + res.status);
  return res.json();
}
const apiQqa = (path, options) => request(PREFIX + path, options);
const apiLegacy = (path, options) => request(LEGACY + path, options);

function setText(sel, text) {
  const el = $(sel);
  if (el) el.textContent = text;
}

function toast(message) {
  let el = $('#toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.remove('show'), 2600);
}

// ── 通用 UI 片段 ─────────────────────────────────────
function card(title, desc, bodyHtml) {
  return `<section class="panel">
    <div class="panel-head"><h3>${esc(title)}</h3>${desc ? `<div class="panel-desc">${esc(desc)}</div>` : ''}</div>
    <div class="panel-body">${bodyHtml}</div>
  </section>`;
}

function switchRow(label, desc, on, attrs) {
  return `<label class="switch-row ${on ? 'on' : ''}">
    <span class="switch-text"><span class="switch-label">${esc(label)}</span>
      ${desc ? `<span class="switch-desc">${esc(desc)}</span>` : ''}</span>
    <input type="checkbox" ${on ? 'checked' : ''} ${attrs || ''} />
    <span class="switch-track"><span class="switch-thumb"></span></span>
  </label>`;
}

function tag(text, kind) {
  return `<span class="tag ${kind || ''}">${esc(text)}</span>`;
}

function groupOptions(selected) {
  if (!state.groups.length) return '<option value="">(没有群)</option>';
  return state.groups
    .map((g) => `<option value="${esc(g.group_id)}" ${String(g.group_id) === String(selected) ? 'selected' : ''}>${esc(g.display_name || g.group_name || g.group_id)}</option>`)
    .join('');
}

function selectedGroup() {
  return state.groups.find((g) => String(g.group_id) === String(state.groupId)) || null;
}

function avatar(member) {
  const url = member && (member.avatar_url || '');
  return `<img class="avatar" src="${esc(url)}" onerror="this.style.visibility='hidden'" alt="" />`;
}

// ── 顶部 / 页签 ──────────────────────────────────────
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme === 'light' ? 'light' : 'dark');
  try { localStorage.setItem('qqa-theme', theme); } catch (e) { /* ignore */ }
}

function switchTab(tab) {
  state.tab = tab;
  $$('#tabs .tab').forEach((btn) => btn.classList.toggle('active', btn.dataset.tab === tab));
  $$('.view').forEach((view) => view.classList.toggle('active', view.id === 'view-' + tab));
  if (tab === 'archive') renderArchivePage();
  if (tab === 'groups') renderGroupsPage();
  if (tab === 'memory') renderMemoryPage();
  if (tab === 'knowledge') renderKnowledgePage();
  if (tab === 'usage') renderUsage();
  if (tab === 'settings') renderSettings();
  if (tab === 'connection') renderConnection();
}

function renderTopbar() {
  const o = state.overview;
  if (!o) return;
  const connected = !!(o.bot && o.bot.connected);
  $('#onebot-dot').className = 'dot ' + (connected ? 'dot-on' : 'dot-off');
  setText('#onebot-label', connected ? 'OneBot 已连接' : 'OneBot 未连接');
  const models = o.models || {};
  setText('#model-label', '模型：' + (models.agent || models.chat || '-'));
  const today = o.today || {};
  setText('#usage-label', `今日：${fmtNum(today.runs)} 次 / ${fmtNum((today.prompt_tokens || 0) + (today.completion_tokens || 0))} tokens`);
  const err = $('#error-label');
  err.textContent = '失败：' + fmtNum(today.errors);
  err.style.color = today.errors ? 'var(--err)' : '';
}

// ── 会话页 ───────────────────────────────────────────
function statusBadge(status) {
  if (status === 'error') return '<span class="badge err">失败</span>';
  if (status === 'running') return '<span class="badge warn">运行中</span>';
  return '<span class="badge ok">成功</span>';
}

function renderRunList() {
  const box = $('#run-items');
  setText('#run-count', `会话 ${state.runs.length} 条`);
  if (!state.runs.length) {
    box.innerHTML = '<div class="empty-hint" style="padding:18px">还没有任何 AI 对话记录。<br>群里有人 @ 猎宝后，这里会出现完整过程。</div>';
    return;
  }
  box.innerHTML = state.runs.map((run) => {
    const active = run.run_id === state.selectedId ? ' active' : '';
    const tokens = (run.usage_prompt_tokens || 0) + (run.usage_completion_tokens || 0);
    return `<div class="session-item${active}" data-run="${esc(run.run_id)}">
      <div class="session-title">${esc(run.trigger_summary || '(无触发摘要)')}</div>
      <div class="session-meta"><span>${esc(run.chat_key || '-')}</span><span>${fmtTime(run.started_at)}</span></div>
      <div class="session-meta">${statusBadge(run.status)}<span>${fmtNum(tokens)} tok</span><span>${fmtNum(run.step_count)} 步</span></div>
    </div>`;
  }).join('');
  $$('#run-items .session-item').forEach((el) => el.addEventListener('click', () => selectRun(el.dataset.run)));
}

function stepCard(step) {
  if (step.kind === 'think') {
    return `<details class="collapsible tool-flow think-flow" open>
      <summary><span class="tool-ico">💭</span><span class="tool-name-flow">思考</span><span class="tool-label">不发送</span></summary>
      <div class="coll-body">${esc(step.result || '')}</div></details>`;
  }
  if (step.kind === 'tool_call') {
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
  if (step.kind === 'sent') {
    return `<details class="collapsible tool-flow sent-flow" open>
      <summary><span class="tool-ico">💬</span><span class="tool-name-flow">已发送到 QQ</span><span class="badge ok">已送出</span></summary>
      <div class="coll-body sent-body">${esc(step.result || '')}</div></details>`;
  }
  if (step.kind === 'retrieval') {
    return `<details class="collapsible tool-flow" open>
      <summary><span class="tool-ico">🔎</span><span class="tool-name-flow">检索</span></summary>
      <div class="coll-body">${esc(JSON.stringify({ args: step.args, result: step.result }, null, 1))}</div></details>`;
  }
  return `<details class="collapsible tool-flow"><summary><span class="tool-ico">•</span><span class="tool-name-flow">${esc(step.kind)}</span></summary><div class="coll-body">${esc(step.result || '')}</div></details>`;
}

function renderRunDetail() {
  const box = $('#run-detail');
  const run = state.detail;
  if (!run) {
    box.innerHTML = '<div class="empty-hint">← 选择左侧会话查看完整过程</div>';
    return;
  }
  const tokens = (run.usage_prompt_tokens || 0) + (run.usage_completion_tokens || 0);
  const html = [`<div class="detail-header">
    <div>
      <div class="detail-title">${esc(run.trigger_summary || '(无摘要)')}</div>
      <div class="session-meta"><span>${esc(run.chat_key)}</span><span>${fmtTime(run.started_at)} → ${fmtTime(run.finished_at)}</span>${statusBadge(run.status)}</div>
    </div>
    <div class="detail-stat">${fmtNum(tokens)} tokens</div>
  </div>`];
  if (run.system_prompt) html.push(`<details class="collapsible"><summary>系统提示（${run.system_prompt.length} 字符）</summary><div class="coll-body">${esc(run.system_prompt)}</div></details>`);
  if (run.user_prompt) html.push(`<details class="collapsible"><summary>本次输入（${run.user_prompt.length} 字符）</summary><div class="coll-body">${esc(run.user_prompt)}</div></details>`);
  html.push('<div class="msg-flow">');
  (run.steps || []).forEach((step) => html.push(stepCard(step)));
  html.push('</div>');
  if (run.error) html.push(`<details class="collapsible tool-flow error-flow" open><summary><span class="tool-ico">⚠</span><span class="tool-name-flow">错误</span></summary><div class="coll-body error-body">${esc(run.error)}</div></details>`);
  html.push(`<div class="session-meta" style="margin-top:10px">
    <span>模型：${esc(run.model || '-')}</span>
    <span>prompt ${fmtNum(run.usage_prompt_tokens)}</span>
    <span>completion ${fmtNum(run.usage_completion_tokens)}</span>
    <span>run_id：${esc(run.run_id)}</span></div>`);
  box.innerHTML = html.join('');
}

async function selectRun(runId) {
  state.selectedId = runId;
  renderRunList();
  const box = $('#run-detail');
  box.innerHTML = '<div class="empty-hint">加载中…</div>';
  try {
    state.detail = await apiQqa('/api/runs/' + encodeURIComponent(runId));
  } catch (e) {
    state.detail = null;
    box.innerHTML = '<div class="empty-hint">加载失败：' + esc(e.message) + '</div>';
    return;
  }
  renderRunDetail();
}

// ── 存档页 ───────────────────────────────────────────
async function ensureGroups() {
  if (state.groups.length) return;
  const data = await apiLegacy('/api/groups');
  state.groups = data.groups || [];
  if (!state.groupId && state.groups.length) state.groupId = String(state.groups[0].group_id);
}

function renderArchivePage() {
  const list = $('#archive-items');
  list.innerHTML = '<div class="list-head muted" style="padding:8px">群</div>' +
    state.groups.map((g) => `<div class="session-item ${String(g.group_id) === String(state.groupId) ? 'active' : ''}" data-gid="${esc(g.group_id)}">
      <div class="session-title">${esc(g.display_name || g.group_name || g.group_id)}</div>
      <div class="session-meta"><span>${esc(g.group_id)}</span><span>${fmtNum(g.message_count)} 条</span></div>
      <div class="session-meta"><span>最后：${esc(g.last_message_at || '-')}</span></div>
    </div>`).join('');
  $$('#archive-items .session-item').forEach((el) => el.addEventListener('click', async () => {
    state.groupId = el.dataset.gid;
    state.archive = { messages: [], offset: 0, hasMore: false, query: '' };
    renderArchivePage();
    await loadArchive(true);
  }));
  if (state.groupId) loadArchive(true);
}

async function loadArchive(reset) {
  if (!state.groupId) return;
  const box = $('#archive-detail');
  if (reset) {
    box.innerHTML = '<div class="empty-hint">加载中…</div>';
  }
  try {
    const query = `?limit=50&offset=${reset ? 0 : state.archive.messages.length}`;
    const data = await apiLegacy(`/api/groups/${encodeURIComponent(state.groupId)}/archive${query}`);
    const rows = (data.recent_messages || []).slice().reverse();
    state.archive.messages = reset ? rows : state.archive.messages.concat(rows);
    state.archive.hasMore = !!data.has_more;
    renderArchiveDetail();
  } catch (e) {
    box.innerHTML = '<div class="empty-hint">加载失败：' + esc(e.message) + '</div>';
  }
}

function renderArchiveDetail() {
  const box = $('#archive-detail');
  const g = selectedGroup();
  const q = state.archive.query.trim().toLowerCase();
  const rows = state.archive.messages.filter((m) => !q || String(m.plain_text || '').toLowerCase().includes(q) || String(m.display_name || '').toLowerCase().includes(q));
  box.innerHTML = `<div class="detail-header">
    <div><div class="detail-title">${esc((g && (g.display_name || g.group_name)) || state.groupId)}</div>
      <div class="session-meta"><span>最后消息 ${esc((g && g.last_message_at) || '-')}</span><span>已加载 ${fmtNum(state.archive.messages.length)} 条</span></div></div>
    <input id="archive-search" class="text-input" placeholder="搜索内容 / 昵称" value="${esc(state.archive.query)}" />
  </div>
  <div class="panel-body" style="padding-top:8px">
    <table class="data-table"><thead><tr><th style="width:120px">时间</th><th style="width:160px">发送者</th><th>内容</th></tr></thead>
    <tbody>${rows.length ? rows.map((m) => `<tr>
      <td class="muted">${esc((m.created_at || '').slice(5, 16))}</td>
      <td>${esc(m.display_name || m.sender_name || m.user_id)}</td>
      <td>${esc(m.preview_text || m.plain_text || '[非文本]')}</td></tr>`).join('')
      : '<tr><td colspan="3" class="muted">没有匹配的消息</td></tr>'}</tbody></table>
    ${state.archive.hasMore ? '<button class="btn btn-small" id="archive-more" style="margin-top:10px">加载更多</button>' : ''}
  </div>`;
  const search = $('#archive-search');
  if (search) search.addEventListener('input', () => { state.archive.query = search.value; renderArchiveDetail(); });
  const more = $('#archive-more');
  if (more) more.addEventListener('click', () => loadArchive(false));
}

// ── 群管理页 ─────────────────────────────────────────
async function loadGroupDetail() {
  if (!state.groupId) return null;
  const [detail, tier] = await Promise.all([
    apiLegacy('/api/groups/' + encodeURIComponent(state.groupId)),
    apiQqa('/api/groups/' + encodeURIComponent(state.groupId) + '/tier').catch(() => null),
  ]);
  state.groupDetail = detail;
  state.tierConfig = tier;
  return state.groupDetail;
}

function runtimeBanner() {
  const runtime = (state.overview || {}).runtime || {};
  if (runtime.enabled) {
    return `<div class="tier-note ok">统一路由已开启；接管群：${(runtime.groups || []).join(' / ') || '（未配置）'}；回怼由 runtime 执行：${runtime.reactions_routed ? '是' : '否'}</div>`;
  }
  return `<div class="tier-note warn">统一路由未启用：回怼仍走旧逻辑，触发档位配置会保存但暂不生效。</div>`;
}

function renderGroupsPage() {
  const page = $('#groups-page');
  if (!state.groupId) {
    page.innerHTML = '<div class="empty-wrap"><div class="empty-hint">还没有配置任何群。</div></div>';
    return;
  }
  page.innerHTML = `<div class="page-wrap">
    <div class="page-head">
      <h2>群管理</h2>
      <div class="page-tools">
        <select id="group-select" class="select-input">${groupOptions(state.groupId)}</select>
        <button class="btn btn-primary" id="group-save">保存本群设置</button>
      </div>
      <div class="page-desc">只影响所选群。改动立即写入数据库，AI 对话的权限与工具开关下一次消息生效。</div>
      ${runtimeBanner()}
    </div>
    <div id="group-body"><div class="empty-hint">加载中…</div></div>
  </div>`;
  $('#group-select').addEventListener('change', async (e) => {
    state.groupId = e.target.value;
    await loadGroupDetail();
    renderGroupsPage();
    renderMemoryPage();
  });
  $('#group-save').addEventListener('click', saveGroupSettings);
  renderGroupBody();
  if (!state.groupDetail) {
    loadGroupDetail().then(renderGroupBody).catch((err) => {
      $('#group-body').innerHTML = `<div class="empty-hint">加载失败：${esc(err.message)}</div>`;
    });
  }
}

function renderGroupBody() {
  const body = $('#group-body');
  const d = state.groupDetail;
  if (!body || !d) return;
  const g = selectedGroup();
  const features = d.features || {};
  const limits = d.limits || {};
  const tools = ((d.agent_tools || {}).tools) || [];
  const featureHtml = featureTreeHtml(FEATURE_TREE, features, 0, true);
  const limitHtml = LIMIT_FEATURES.map(([key, label]) => {
    const l = limits[key] || { per_minute: 0, per_hour: 0, per_day: 0 };
    return `<div class="limit-row">
      <div class="limit-label">${esc(label)}</div>
      <label>每分钟 <input class="num-input" type="number" min="0" data-limit="${key}:per_minute" value="${esc(l.per_minute)}" /></label>
      <label>每小时 <input class="num-input" type="number" min="0" data-limit="${key}:per_hour" value="${esc(l.per_hour)}" /></label>
      <label>每天 <input class="num-input" type="number" min="0" data-limit="${key}:per_day" value="${esc(l.per_day)}" /></label>
    </div>`;
  }).join('');
  const toolHtml = tools.map((tool) => {
    const disabled = !tool.configurable;
    const notes = [];
    if (tool.requires_admin) notes.push('管理员');
    if (tool.requires_feature) notes.push('依赖功能：' + tool.requires_feature);
    if (tool.requires_group) notes.push('仅群聊');
    if (disabled) notes.push('不可配置');
    return `<div class="tool-row">
      <div class="tool-main"><div class="tool-name">${esc(tool.label || tool.name)} ${tag(tool.category || '工具', 'plain')}</div>
        <div class="tool-desc">${esc(tool.description || '')} ${notes.map((n) => tag(n, 'plain')).join(' ')}</div></div>
      <label class="switch-mini ${tool.enabled ? 'on' : ''}"><input type="checkbox" ${tool.enabled ? 'checked' : ''} ${disabled ? 'disabled' : ''} data-tool="${esc(tool.name)}" /><span></span></label>
    </div>`;
  }).join('');
  const chime = d.chime || { enabled: false, mode: 'hourly' };
  body.innerHTML = `
    <div class="info-strip">
      <span>群号 ${esc(d.group_id)}</span>
      <span>${esc((g && (g.display_name || g.group_name)) || '')}</span>
      <span>归档 ${fmtNum((g && g.message_count) || 0)} 条</span>
    </div>
    ${card('功能开关（分层）', '上层关闭时下层自动失效。回怼类为确定性规则，命中后不会进入 LLM；触发档位默认关闭。', `<div class="switch-list">${featureHtml}</div>`)}
    ${card('发言限流', '只对三类“主动/被动反应”生效，防止刷屏；0 表示不限制。', `<div class="limit-list">${limitHtml}</div>`)}
    ${card('常数报时', '整点播报，管理员在群里用命令也能开关。', `<div class="form-row">
        ${switchRow('启用报时', '', !!chime.enabled, 'data-chime="enabled"')}
        <label class="field-inline">模式
          <select class="select-input" id="chime-mode">
            <option value="hourly" ${chime.mode === 'hourly' ? 'selected' : ''}>每小时 58 分</option>
            <option value="twice_daily" ${chime.mode === 'twice_daily' ? 'selected' : ''}>每天 1:58 / 13:58</option>
          </select></label></div>`)}
    ${card('AI 工具权限', '决定该群里 AI 能调用哪些工具。灰掉的工具由代码强制（依赖未开启的功能或权限）。', `<div class="tool-list">${toolHtml}</div>`)}
    ${card('群画像', '这段文字会作为该群的背景资料注入 AI 上下文，用来约束回复风格。', `<textarea id="group-profile" class="text-area" placeholder="例如：这是一个技术交流群，氛围轻松，喜欢用梗。">${esc((d.group_profile || {}).summary || '')}</textarea>`)}
  `;
}

async function saveGroupSettings() {
  if (!state.groupId) return;
  const features = {};
  $$('[data-feature]').forEach((el) => { features[el.dataset.feature] = el.checked; });
  const chimeEnabled = $('[data-chime="enabled"]');
  if (chimeEnabled) features.hourly_chime = chimeEnabled.checked;
  const limits = {};
  $$('[data-limit]').forEach((el) => {
    const [feature, field] = el.dataset.limit.split(':');
    limits[feature] = limits[feature] || {};
    limits[feature][field] = Number(el.value || 0);
  });
  const agentTools = {};
  $$('[data-tool]').forEach((el) => { if (!el.disabled) agentTools[el.dataset.tool] = el.checked; });
  const chimeMode = ($('#chime-mode') || {}).value || 'hourly';
  const groupProfile = ($('#group-profile') || {}).value || '';
  try {
    const payload = {
      features,
      limits,
      agent_tools: agentTools,
      chime: { mode: chimeMode },
      group_profile: { summary: groupProfile, max_chars: 100 },
    };
    const data = await apiLegacy('/api/groups/' + encodeURIComponent(state.groupId), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    state.groupDetail = data;
    const tierToggle = $('[data-tier="enabled"]');
    const tierPayload = {
      enabled: tierToggle ? tierToggle.checked : !!(state.tierConfig || {}).enabled,
      keywords: (($('#tier-keywords') || {}).value || ''),
      probability: Number((($('#tier-probability') || {}).value || 0)),
      cooldown_minutes: Number((($('#tier-cooldown') || {}).value || 0)),
      daily_limit: Number((($('#tier-daily') || {}).value || 0)),
    };
    state.tierConfig = await apiQqa('/api/groups/' + encodeURIComponent(state.groupId) + '/tier', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(tierPayload),
    });
    renderGroupBody();
    toast('已保存');
  } catch (e) {
    toast('保存失败：' + e.message);
  }
}

// ── 画像页 ───────────────────────────────────────────
function renderMemoryPage() {
  const list = $('#member-items');
  const members = state.members;
  const q = state.memberQuery.trim().toLowerCase();
  const filtered = members
    .filter((m) => !q || String(m.display_name || '').toLowerCase().includes(q) || String(m.user_id).includes(q))
    .slice(0, 80);
  const targets = members.filter((m) => m.target_enabled).length;
  list.innerHTML = `<div class="list-head-block">
      <select id="memory-group" class="select-input">${groupOptions(state.groupId)}</select>
      <input id="memory-search" class="text-input" placeholder="搜索昵称 / QQ" value="${esc(state.memberQuery)}" />
      <div class="list-hint muted">已记录 ${targets} 人 / 共 ${fmtNum(members.length)} 人${members.length > 80 && !q ? '（显示前 80，输入搜索查看全部）' : ''}</div>
    </div>` +
    (filtered.length ? filtered.map((m) => `<div class="session-item ${String(m.user_id) === String(state.memberSelected) ? 'active' : ''}" data-uid="${esc(m.user_id)}">
      <div class="member-line">${avatar(m)}<span class="session-title">${esc(m.display_name || m.nickname || m.user_id)}</span></div>
      <div class="session-meta"><span>${esc(m.user_id)}</span>${m.target_enabled ? '<span class="badge ok">记录中</span>' : '<span class="badge plain">未记录</span>'}</div>
    </div>`).join('') : '<div class="empty-hint" style="padding:14px">没有匹配的群友</div>');
  $('#memory-group').addEventListener('change', async (e) => {
    state.groupId = e.target.value;
    state.memberSelected = '';
    state.companion = null;
    await loadGroupDetail();
    applyMembers();
    renderMemoryPage();
  });
  const search = $('#memory-search');
  search.addEventListener('input', () => { state.memberQuery = search.value; renderMemoryPage(); });
  $$('#member-items .session-item').forEach((el) => el.addEventListener('click', () => selectMember(el.dataset.uid)));
  if (state.memberSelected) renderMemberDetail(); else $('#member-detail').innerHTML = '<div class="empty-hint">← 选择左侧群友查看画像</div>';
}

function applyMembers() {
  state.members = ((state.groupDetail || {}).members) || [];
}

async function selectMember(uid) {
  state.memberSelected = String(uid);
  renderMemoryPage();
  const box = $('#member-detail');
  box.innerHTML = '<div class="empty-hint">加载中…</div>';
  try {
    state.companion = await apiLegacy(`/api/groups/${encodeURIComponent(state.groupId)}/companions/${encodeURIComponent(uid)}`);
    renderMemberDetail();
  } catch (e) {
    state.companion = null;
    box.innerHTML = '<div class="empty-hint">加载失败：' + esc(e.message) + '</div>';
  }
}

function renderMemberDetail() {
  const box = $('#member-detail');
  const c = state.companion;
  if (!c) { box.innerHTML = '<div class="empty-hint">← 选择左侧群友查看画像</div>'; return; }
  const m = c.member || {};
  const p = c.profile || {};
  const target = c.target || {};
  const topics = Array.isArray(p.topics) ? p.topics.join(', ') : (p.topics || '');
  box.innerHTML = `<div class="detail-header">
    <div class="member-line">${avatar(m)}
      <div><div class="detail-title">${esc(m.display_name || m.nickname || m.user_id)}</div>
      <div class="session-meta"><span>QQ ${esc(m.user_id)}</span><span>${esc(m.role || 'member')}</span>${target.enabled ? '<span class="badge ok">记录中</span>' : '<span class="badge plain">未记录</span>'}</div></div>
    </div>
    <label class="switch-mini ${target.enabled ? 'on' : ''}" title="开启后自动总结此人画像"><input type="checkbox" id="target-toggle" ${target.enabled ? 'checked' : ''} /><span></span></label>
  </div>
  <div class="panel-body">
    ${card('画像字段', '这些字段会注入 AI 的上下文，建议简短、客观。',
      `<label class="field"><span>画像摘要</span><input class="text-input" id="p-summary" value="${esc(p.summary || '')}" /></label>
       <label class="field"><span>近期在做什么</span><input class="text-input" id="p-activity" value="${esc(p.current_activity || '')}" /></label>
       <label class="field"><span>性格 / 互动风格</span><input class="text-input" id="p-personality" value="${esc(p.personality_notes || '')}" /></label>
       <label class="field"><span>情绪偏好 / 雷点</span><input class="text-input" id="p-emotion" value="${esc(p.emotional_preferences || '')}" /></label>
       <label class="field"><span>常聊主题（逗号分隔）</span><input class="text-input" id="p-topics" value="${esc(topics)}" /></label>
       <label class="field"><span>长期画像</span><textarea class="text-area" id="p-longterm">${esc(p.longterm_profile || '')}</textarea></label>
       <div class="btn-row"><button class="btn btn-primary" id="memory-save">保存画像</button>
         <button class="btn btn-danger" id="memory-reset">清空画像</button></div>`)}
    <div class="panel-desc">最后更新：${esc(p.updated_at || '从未')}　置信度：${esc(p.confidence == null ? '-' : p.confidence)}</div>
  </div>`;
  $('#target-toggle').addEventListener('change', async (e) => {
    try {
      await apiLegacy(`/api/groups/${encodeURIComponent(state.groupId)}/companions/${encodeURIComponent(state.memberSelected)}/target`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: e.target.checked }),
      });
      toast(e.target.checked ? '已加入记录' : '已停止记录');
      await loadGroupDetail();
      applyMembers();
      state.companion = await apiLegacy(`/api/groups/${encodeURIComponent(state.groupId)}/companions/${encodeURIComponent(state.memberSelected)}`);
      renderMemberDetail();
    } catch (err) { toast('操作失败：' + err.message); }
  });
  $('#memory-save').addEventListener('click', async () => {
    const payload = {
      summary: ($('#p-summary') || {}).value || '',
      current_activity: ($('#p-activity') || {}).value || '',
      personality_notes: ($('#p-personality') || {}).value || '',
      emotional_preferences: ($('#p-emotion') || {}).value || '',
      topics: (($('#p-topics') || {}).value || '').split(',').map((s) => s.trim()).filter(Boolean),
      longterm_profile: ($('#p-longterm') || {}).value || '',
    };
    try {
      await apiLegacy(`/api/groups/${encodeURIComponent(state.groupId)}/companions/${encodeURIComponent(state.memberSelected)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
      toast('画像已保存');
      state.companion = await apiLegacy(`/api/groups/${encodeURIComponent(state.groupId)}/companions/${encodeURIComponent(state.memberSelected)}`);
      renderMemberDetail();
    } catch (e) { toast('保存失败：' + e.message); }
  });
  $('#memory-reset').addEventListener('click', async () => {
    if (!confirm('确定清空这个群友的画像与记忆？此操作不可撤销。')) return;
    try {
      await apiLegacy(`/api/groups/${encodeURIComponent(state.groupId)}/companions/${encodeURIComponent(state.memberSelected)}/reset`, { method: 'POST' });
      toast('已清空');
      state.companion = await apiLegacy(`/api/groups/${encodeURIComponent(state.groupId)}/companions/${encodeURIComponent(state.memberSelected)}`);
      renderMemberDetail();
    } catch (e) { toast('操作失败：' + e.message); }
  });
}

// ── 知识库页 ─────────────────────────────────────────
async function loadKnowledge() {
  const data = await apiLegacy('/api/knowledge');
  state.knowledgeKinds = data.kind_order || [];
  const tree = (data.tree || {}).STS2 || {};
  const items = [];
  Object.entries(tree).forEach(([kind, list]) => {
    (list || []).forEach((item) => items.push({ ...item, category: kind }));
  });
  state.knowledge = items;
}

function renderKnowledgePage() {
  const list = $('#knowledge-items');
  const kinds = state.knowledgeKinds.length ? state.knowledgeKinds : ['card'];
  const q = state.knowledgeQuery.trim().toLowerCase();
  const items = state.knowledge
    .filter((item) => item.category === state.knowledgeKind || !state.knowledgeKind)
    .filter((item) => !q || String(item.title || '').toLowerCase().includes(q) || String(item.content || '').toLowerCase().includes(q))
    .slice(0, 120);
  list.innerHTML = `<div class="list-head-block">
      <select id="k-kind" class="select-input">${kinds.map((k) => `<option value="${esc(k)}" ${k === state.knowledgeKind ? 'selected' : ''}>${esc(k)}</option>`).join('')}</select>
      <input id="k-search" class="text-input" placeholder="搜索标题 / 内容" value="${esc(state.knowledgeQuery)}" />
      <div class="btn-row" style="padding:6px 10px"><button class="btn btn-small" id="k-new">新建条目</button>
        <button class="btn btn-small btn-danger" id="k-rebuild">重建 STS2</button></div>
    </div>` +
    (items.length ? items.map((item) => `<div class="session-item ${String(item.id) === String(state.knowledgeSelected && state.knowledgeSelected.id) ? 'active' : ''}" data-kid="${esc(item.id)}">
      <div class="session-title">${esc(item.title || '(无标题)')}</div>
      <div class="session-meta"><span>#${esc(item.id)}</span><span>${esc(item.category)}</span></div>
    </div>`).join('') : '<div class="empty-hint" style="padding:14px">没有匹配的条目</div>');
  $('#k-kind').addEventListener('change', (e) => { state.knowledgeKind = e.target.value; renderKnowledgePage(); });
  $('#k-search').addEventListener('input', (e) => { state.knowledgeQuery = e.target.value; renderKnowledgePage(); });
  $('#k-new').addEventListener('click', () => { state.knowledgeSelected = { id: null, title: '', category: state.knowledgeKind, keywords: '[]', content: '', enabled: true }; renderKnowledgeDetail(); });
  $('#k-rebuild').addEventListener('click', async () => {
    if (!confirm('重建 STS2 知识库会清空并重新导入 STS1/STS2 条目（手写条目不受影响），耗时较长。继续？')) return;
    try { await apiLegacy('/api/knowledge/seed-sts', { method: 'POST' }); toast('已开始重建'); await loadKnowledge(); renderKnowledgePage(); }
    catch (e) { toast('重建失败：' + e.message); }
  });
  $$('#knowledge-items .session-item').forEach((el) => el.addEventListener('click', async () => {
    try {
      const data = await apiLegacy('/api/knowledge/' + encodeURIComponent(el.dataset.kid));
      state.knowledgeSelected = { ...data.item, category: data.item.category };
      renderKnowledgeDetail();
    } catch (e) { toast('加载失败：' + e.message); }
  }));
  if (!state.knowledge.length) {
    loadKnowledge().then(renderKnowledgePage).catch((e) => { list.innerHTML = `<div class="empty-hint">加载失败：${esc(e.message)}</div>`; });
  }
}

function renderKnowledgeDetail() {
  const box = $('#knowledge-detail');
  const item = state.knowledgeSelected;
  if (!item) { box.innerHTML = '<div class="empty-hint">← 选择左侧条目查看/编辑</div>'; return; }
  const isNew = item.id == null;
  box.innerHTML = `<div class="page-wrap">
    <div class="page-head"><h2>${isNew ? '新建知识条目' : '编辑知识条目 #' + esc(item.id)}</h2></div>
    <div class="panel"><div class="panel-body">
      <label class="field"><span>标题</span><input class="text-input" id="kd-title" value="${esc(item.title || '')}" /></label>
      <label class="field"><span>分类</span><input class="text-input" id="kd-category" value="${esc(item.category || '')}" placeholder="card / relic / guide ..." /></label>
      <label class="field"><span>关键词（JSON 数组或逗号分隔）</span><input class="text-input" id="kd-keywords" value="${esc(typeof item.keywords === 'string' ? item.keywords : JSON.stringify(item.keywords || []))}" /></label>
      <label class="field"><span>正文</span><textarea class="text-area" id="kd-content" style="min-height:260px">${esc(item.content || '')}</textarea></label>
      <label class="field-inline"><input type="checkbox" id="kd-enabled" ${item.enabled !== false ? 'checked' : ''} /> 启用</label>
      <div class="btn-row"><button class="btn btn-primary" id="kd-save">保存</button>
        ${isNew ? '' : '<button class="btn btn-danger" id="kd-delete">删除</button>'}</div>
    </div></div></div>`;
  $('#kd-save').addEventListener('click', async () => {
    const payload = {
      title: $('#kd-title').value,
      category: $('#kd-category').value,
      keywords: $('#kd-keywords').value,
      content: $('#kd-content').value,
      enabled: $('#kd-enabled').checked,
    };
    try {
      if (isNew) await apiLegacy('/api/knowledge', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      else await apiLegacy('/api/knowledge/' + encodeURIComponent(item.id), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      toast('已保存');
      await loadKnowledge();
      renderKnowledgePage();
    } catch (e) { toast('保存失败：' + e.message); }
  });
  if (!isNew) {
    $('#kd-delete').addEventListener('click', async () => {
      if (!confirm('确定删除这条知识？')) return;
      try { await apiLegacy('/api/knowledge/' + encodeURIComponent(item.id), { method: 'DELETE' }); toast('已删除'); state.knowledgeSelected = null; await loadKnowledge(); renderKnowledgePage(); renderKnowledgeDetail(); }
      catch (e) { toast('删除失败：' + e.message); }
    });
  }
}

// ── 用量 / 设置 / 连接 ───────────────────────────────
function renderUsage() {
  const page = $('#usage-page');
  const o = state.overview || {};
  const today = o.today || {};
  const stats = o.stats || {};
  const total = (today.prompt_tokens || 0) + (today.completion_tokens || 0);
  page.innerHTML = `<div class="page-wrap">
    <div class="page-head"><h2>用量</h2><div class="page-desc">只统计 AI 对话；成本核算在 P3。</div></div>
    <div class="usage-cards">
      <div class="usage-card accent"><div class="uc-label">今日 tokens</div><div class="uc-value">${fmtNum(total)}</div>
        <div class="uc-sub">prompt ${fmtNum(today.prompt_tokens)} / completion ${fmtNum(today.completion_tokens)}</div></div>
      <div class="usage-card"><div class="uc-label">今日对话</div><div class="uc-value">${fmtNum(today.runs)}</div><div class="uc-sub">失败 ${fmtNum(today.errors)}</div></div>
      <div class="usage-card"><div class="uc-label">累计对话</div><div class="uc-value">${fmtNum(stats.total)}</div><div class="uc-sub">累计失败 ${fmtNum(stats.errors)}</div></div>
    </div>
    <table class="data-table" style="margin-top:18px"><thead><tr><th>时间</th><th>会话</th><th>触发</th><th>tokens</th><th>状态</th></tr></thead>
    <tbody>${(state.runs || []).map((r) => `<tr><td class="muted">${fmtTime(r.started_at)}</td><td>${esc(r.chat_key)}</td><td>${esc(r.trigger_summary || '')}</td>
      <td>${fmtNum((r.usage_prompt_tokens || 0) + (r.usage_completion_tokens || 0))}</td><td>${esc(r.status)}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">暂无数据</td></tr>'}</tbody></table>
  </div>`;
}

async function renderSettings() {
  const page = $('#settings-page');
  page.innerHTML = `<div class="page-wrap">
    <div class="page-head"><h2>设置</h2><div class="page-desc">人设保存后写入 data/bot_persona_prompt.txt，AI 对话实时读取。</div></div>
    <div class="panel"><div class="panel-body">
      <textarea id="persona-text" class="text-area" style="min-height:320px"></textarea>
      <div class="btn-row"><button class="btn btn-primary" id="persona-save">保存</button><span id="persona-status" class="muted"></span></div>
    </div></div></div>`;
  try {
    const data = await apiLegacy('/api/state');
    $('#persona-text').value = data.persona || '';
  } catch (e) { setText('#persona-status', '加载失败：' + e.message); }
  $('#persona-save').addEventListener('click', async () => {
    setText('#persona-status', '保存中…');
    try {
      await apiLegacy('/api/persona', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ persona: $('#persona-text').value }) });
      setText('#persona-status', '已保存');
    } catch (e) { setText('#persona-status', '保存失败：' + e.message); }
  });
}

function renderConnection() {
  const page = $('#connection-page');
  const o = state.overview || {};
  const bot = o.bot || {};
  const models = o.models || {};
  page.innerHTML = `<div class="page-wrap">
    <div class="page-head"><h2>连接</h2><div class="page-desc">运行环境与实时通道状态。</div></div>
    <div class="panel"><div class="panel-body">
      <table class="data-table"><tbody>
        <tr><th style="width:200px">OneBot</th><td>${bot.connected ? '<span class="badge ok">已连接</span>' : '<span class="badge err">未连接</span>'}　QQ：${esc(bot.self_id || '-')}</td></tr>
        <tr><th>版本</th><td>${esc(o.version || '-')}</td></tr>
        <tr><th>服务器时间</th><td>${esc(o.server_time || '-')}</td></tr>
        <tr><th>对话模型</th><td>${esc(models.chat || '-')}</td></tr>
        <tr><th>Agent 模型</th><td>${esc(models.agent || '-')}</td></tr>
        <tr><th>总结模型</th><td>${esc(models.summary || '-')}</td></tr>
        <tr><th>SSE 实时通道</th><td>${state.sseOk ? '<span class="badge ok">正常</span>' : '<span class="badge warn">降级为轮询</span>'}</td></tr>
      </tbody></table>
    </div></div></div>`;
}

// ── 数据刷新 ─────────────────────────────────────────
function applySnapshot(payload) {
  if (!payload) return;
  if (payload.overview) state.overview = payload.overview;
  if (Array.isArray(payload.runs)) state.runs = payload.runs;
  renderTopbar();
  if (state.tab === 'runs') renderRunList();
  if (state.tab === 'usage') renderUsage();
  if (state.tab === 'connection') renderConnection();
}

async function refreshOnce() {
  try {
    const [overview, runs] = await Promise.all([apiQqa('/api/overview'), apiQqa('/api/runs?limit=50')]);
    state.sseOk = false;
    applySnapshot({ overview, runs: runs.runs });
  } catch (e) { /* 静默：未登录时页面保持空态 */ }
}

function connectEvents() {
  if (!window.EventSource) return;
  let es;
  try { es = new EventSource(PREFIX + '/api/events'); } catch (e) { return; }
  es.onopen = () => { state.sseOk = true; if (state.tab === 'connection') renderConnection(); };
  es.onmessage = (event) => { try { applySnapshot(JSON.parse(event.data)); } catch (e) { /* ignore */ } };
  es.onerror = () => { state.sseOk = false; es.close(); setTimeout(connectEvents, 5000); };
}

function init() {
  applyTheme(localStorage.getItem('qqa-theme') || 'dark');
  $$('#tabs .tab').forEach((btn) => btn.addEventListener('click', () => switchTab(btn.dataset.tab)));
  $('#theme-btn').addEventListener('click', () => {
    applyTheme(document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light');
  });
  refreshOnce();
  connectEvents();
  setInterval(() => { if (!state.sseOk) refreshOnce(); }, 5000);
  ensureGroups()
    .then(async () => { await loadGroupDetail(); applyMembers(); renderGroupsPage(); renderMemoryPage(); renderArchivePage(); renderKnowledgePage(); })
    .catch(() => { /* 页面按需重试 */ });
}

document.addEventListener('DOMContentLoaded', init);
