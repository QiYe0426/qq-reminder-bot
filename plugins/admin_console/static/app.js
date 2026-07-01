const token = new URLSearchParams(location.search).get("token") || "";
const API = "/hunterbot/admin-console";
const THEME_STORAGE_KEY = "hunterbot_admin_theme";

const state = {
  view: "persona",
  data: null,
  selectedTree: "STS2",
  selectedKind: "card",
  selectedKnowledgeId: null,
  knowledgeDetail: null,
  knowledgeSearch: "",
  selectedGroupId: "",
  group: null,
  selectedCompanionId: null,
  companionDetail: null,
  companionSearch: "",
  companionListScrollTop: 0,
  selectedCompanionIds: new Set(),
  archiveExpanded: false,
  archiveFilterUserId: "",
  archiveRows: [],
  archiveHasMore: false,
  archiveOffset: 0,
};

const ARCHIVE_PAGE_SIZE = 50;

const labels = {
  card: "卡牌",
  character: "角色",
  relic: "遗物",
  potion: "药水",
  enemy: "怪物",
  elite: "精英",
  boss: "Boss",
  event: "事件",
  mechanic: "机制",
  keyword: "关键词",
  power: "能力",
  enchantment: "附魔",
  guide: "攻略",
  other: "其他",
  ai_chat: "AI 对话",
  daily_report: "日报",
  daily_report_auto: "自动发送日报",
  companion: "智能陪伴",
  hourly_chime: "🎒常数报时",
  bot_tease: "调戏其他bot",
  constant_retort: "🎒常数回怼",
};

const $ = (id) => document.getElementById(id);

function preferredTheme() {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    // Browser storage can be unavailable in restricted webviews.
  }
  if (window.matchMedia?.("(prefers-color-scheme: dark)")?.matches) return "dark";
  return "light";
}

function applyTheme(theme) {
  const mode = theme === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = mode;
  document.querySelectorAll("[data-theme-option]").forEach((node) => {
    node.classList.toggle("active", node.dataset.themeOption === mode);
    node.setAttribute("aria-pressed", node.dataset.themeOption === mode ? "true" : "false");
  });
}

function setTheme(theme) {
  const mode = theme === "dark" ? "dark" : "light";
  try {
    localStorage.setItem(THEME_STORAGE_KEY, mode);
  } catch {
    // The visual switch should still work even if the preference cannot be saved.
  }
  applyTheme(mode);
}

function status(message, isError = false) {
  const node = $("status");
  node.textContent = message || "";
  node.style.color = isError ? "var(--danger)" : "var(--muted)";
}

async function api(path, options = {}) {
  const separator = path.includes("?") ? "&" : "?";
  const response = await fetch(`${path}${separator}token=${encodeURIComponent(token)}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch {
      // Keep the HTTP status fallback.
    }
    throw new Error(detail);
  }
  return response.json();
}

function setTitle(title, subtitle) {
  $("pageTitle").textContent = title;
  $("pageSubtitle").textContent = subtitle;
}

function syncCompanionMemberColumnHeight() {
  // Height is controlled by CSS so the member list and editor scroll independently.
}

function navButton(view, label) {
  const button = document.querySelector(`[data-view="${view}"]`);
  if (!button) return;
  button.classList.toggle("active", state.view === view);
  button.textContent = label || button.textContent;
}

function refreshNav() {
  navButton("persona");
  navButton("knowledge");
  navButton("groups");
}

function setView(view) {
  state.view = view;
  state.selectedKnowledgeId = null;
  state.knowledgeDetail = null;
  state.selectedCompanionId = null;
  state.companionDetail = null;
  refreshNav();
  render();
}

function button(className, text, onClick) {
  const node = document.createElement("button");
  node.className = className;
  node.type = "button";
  node.textContent = text;
  node.onclick = onClick;
  return node;
}

function withToken(path) {
  const separator = path.includes("?") ? "&" : "?";
  return `${path}${separator}token=${encodeURIComponent(token)}`;
}

function sideButton({ title, meta, active, onClick, avatarUrl }) {
  const node = document.createElement("button");
  node.className = `side-button${avatarUrl ? " with-avatar" : ""}${active ? " active" : ""}`;
  node.type = "button";
  node.onclick = onClick;

  if (avatarUrl) {
    const avatar = document.createElement("img");
    avatar.className = "side-avatar";
    avatar.src = withToken(avatarUrl);
    avatar.alt = "";
    avatar.loading = "lazy";
    avatar.decoding = "async";
    avatar.onerror = () => {
      avatar.remove();
      node.classList.remove("with-avatar");
    };
    node.appendChild(avatar);
  }

  const textWrap = document.createElement("div");
  textWrap.className = "side-text";

  const titleNode = document.createElement("div");
  titleNode.className = "side-title";
  titleNode.textContent = title || "未命名";

  const metaNode = document.createElement("div");
  metaNode.className = "side-meta";
  metaNode.textContent = meta || "";

  textWrap.append(titleNode, metaNode);
  node.appendChild(textWrap);
  return node;
}

function field({ id, label, value = "", rows = 1, wide = false, type = "text" }) {
  const wrap = document.createElement("div");
  wrap.className = `field${wide ? " wide" : ""}`;

  const labelNode = document.createElement("label");
  labelNode.htmlFor = id;
  labelNode.textContent = label;

  const input = document.createElement(rows > 1 ? "textarea" : "input");
  input.id = id;
  if (rows > 1) input.rows = rows;
  if (rows <= 1) input.type = type;
  input.value = value ?? "";

  wrap.append(labelNode, input);
  return wrap;
}

function readInput(id) {
  const node = $(id);
  return node ? node.value : "";
}

function readNumber(id, fallback = 0) {
  const value = Number.parseInt(readInput(id), 10);
  return Number.isFinite(value) ? Math.max(0, Math.min(value, 999)) : fallback;
}

function parseKeywordInput(value) {
  return String(value || "")
    .split(/[,，、\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function visibleMemberIds() {
  return filteredMembers().map((member) => String(member.user_id || "")).filter(Boolean);
}

function selectedVisibleMemberIds() {
  const visible = new Set(visibleMemberIds());
  return [...state.selectedCompanionIds].filter((userId) => visible.has(userId));
}

function pruneSelectedCompanionIds() {
  const members = new Set((state.group?.members || []).map((member) => String(member.user_id || "")).filter(Boolean));
  for (const userId of [...state.selectedCompanionIds]) {
    if (!members.has(userId)) state.selectedCompanionIds.delete(userId);
  }
}

function renderSidePanel() {
  const panel = $("sidePanel");
  panel.innerHTML = "";

  if (state.view === "persona") {
    const heading = document.createElement("div");
    heading.className = "side-heading";
    heading.textContent = "全局设置";
    panel.appendChild(heading);
    panel.appendChild(sideButton({
      title: "Bot 人设",
      meta: "全局回复风格",
      active: true,
      onClick: () => {},
    }));
    return;
  }

  if (state.view === "knowledge") {
    const treeHeading = document.createElement("div");
    treeHeading.className = "side-heading";
    treeHeading.textContent = "知识库";
    panel.appendChild(treeHeading);

    for (const tree of ["STS2"]) {
      panel.appendChild(sideButton({
        title: "杀戮尖塔 2",
        meta: "事实库 + 自建攻略库",
        active: state.selectedTree === tree,
        onClick: () => {
          state.selectedTree = tree;
          state.selectedKnowledgeId = null;
          state.knowledgeDetail = null;
          render();
        },
      }));
    }
    return;
  }

  if (state.view === "groups") {
    const heading = document.createElement("div");
    heading.className = "side-heading";
    heading.textContent = "群列表";
    panel.appendChild(heading);

    const groups = (state.data && state.data.groups) || [];
    if (!groups.length) {
      const empty = document.createElement("div");
      empty.className = "muted";
      empty.textContent = "暂无群数据。";
      panel.appendChild(empty);
      return;
    }
    for (const group of groups) {
      panel.appendChild(sideButton({
        title: group.display_name || `群 ${group.group_id}`,
        meta: `${group.group_id}${group.last_message_at ? ` / ${group.last_message_at}` : ""}`,
        avatarUrl: group.avatar_url,
        active: state.selectedGroupId === group.group_id,
        onClick: async () => {
          state.selectedGroupId = group.group_id;
          state.selectedCompanionId = null;
          state.companionDetail = null;
          resetArchiveView();
          await loadGroup(group.group_id);
          render();
        },
      }));
    }
  }
}

function renderPersona() {
  setTitle("Bot 人设", "编辑猎宝全局回复风格和设定。保存后云端立即生效。");
  const content = $("content");
  content.innerHTML = "";

  const panel = document.createElement("div");
  panel.className = "panel section persona-editor";
  panel.appendChild(field({
    id: "personaText",
    label: "人设提示词",
    value: (state.data && state.data.persona) || "",
    rows: 22,
    wide: true,
  }));
  content.appendChild(panel);
}

function knowledgeItems() {
  const tree = state.data?.knowledge?.tree || {};
  const bucket = tree[state.selectedTree] || {};
  const rows = bucket[state.selectedKind] || [];
  const query = state.knowledgeSearch.trim().toLowerCase();
  if (!query) return rows;
  return rows.filter((item) => {
    const text = `${item.title || ""} ${item.category || ""} ${item.keywords || ""}`.toLowerCase();
    return text.includes(query);
  });
}

function renderKnowledge() {
  setTitle("杀戮尖塔 2 知识库", "事实资料和自建攻略分区管理；AI 只会读取与当前问题相关的条目。");
  const content = $("content");
  content.innerHTML = "";

  const layout = document.createElement("div");
  layout.className = "knowledge-layout";

  const listPanel = document.createElement("div");
  listPanel.className = "panel section";

  const tabs = document.createElement("div");
  tabs.className = "tabs";
  const kindOrder = state.data?.knowledge?.kind_order || [
    "card",
    "character",
    "relic",
    "potion",
    "enemy",
    "elite",
    "boss",
    "event",
    "mechanic",
    "keyword",
    "power",
    "enchantment",
    "guide",
  ];
  for (const kind of kindOrder) {
    tabs.appendChild(button(`tab${state.selectedKind === kind ? " active" : ""}`, labels[kind] || kind, () => {
      state.selectedKind = kind;
      state.selectedKnowledgeId = null;
      state.knowledgeDetail = null;
      render();
    }));
  }

  const toolbar = document.createElement("div");
  toolbar.className = "toolbar-row";
  const search = document.createElement("input");
  search.className = "search";
  search.placeholder = "搜索标题、分类、关键词";
  search.value = state.knowledgeSearch;
  search.oninput = () => {
    state.knowledgeSearch = search.value;
    renderKnowledgeList();
  };
  toolbar.appendChild(search);
  toolbar.appendChild(button("button", "新增", () => {
    state.selectedKnowledgeId = "new";
    state.knowledgeDetail = {
      item: {
        title: "",
        category: `${state.selectedTree}/${state.selectedKind}`,
        keywords: "[]",
        content: "",
        enabled: 1,
      },
    };
    render();
  }));

  const list = document.createElement("div");
  list.id = "knowledgeList";
  list.className = "item-list";

  listPanel.append(tabs, toolbar, list);

  const editorPanel = document.createElement("div");
  editorPanel.className = "panel section";
  editorPanel.id = "knowledgeEditor";

  layout.append(listPanel, editorPanel);
  content.appendChild(layout);
  renderKnowledgeList();
  renderKnowledgeEditor();
}

function renderKnowledgeList() {
  const list = $("knowledgeList");
  if (!list) return;
  list.innerHTML = "";
  const items = knowledgeItems();
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "当前分类暂无条目。";
    list.appendChild(empty);
    return;
  }
  for (const item of items) {
    const active = state.selectedKnowledgeId === item.id;
    const node = document.createElement("button");
    node.className = `item-button${active ? " active" : ""}`;
    node.type = "button";
    node.onclick = async () => {
      state.selectedKnowledgeId = item.id;
      state.knowledgeDetail = await api(`${API}/api/knowledge/${item.id}`);
      render();
    };
    node.innerHTML = `<div class="side-title"></div><div class="side-meta"></div>`;
    node.children[0].textContent = item.title || "未命名";
    node.children[1].textContent = `${item.enabled ? "启用" : "停用"} / ${item.category || "未分类"}`;
    list.appendChild(node);
  }
}

function renderKnowledgeEditor() {
  const panel = $("knowledgeEditor");
  if (!panel) return;
  panel.innerHTML = "";
  const detail = state.knowledgeDetail;
  if (!detail) {
    panel.appendChild(empty("选择左侧条目查看或编辑。"));
    return;
  }
  const item = detail.item || {};
  const grid = document.createElement("div");
  grid.className = "form-grid";
  grid.append(
    field({ id: "knowledgeTitle", label: "标题", value: item.title || "" }),
    field({ id: "knowledgeCategory", label: "分类", value: item.category || `${state.selectedTree}/${state.selectedKind}` }),
    field({ id: "knowledgeKeywords", label: "关键词", value: parseJsonArray(item.keywords).join("，"), rows: 3, wide: true }),
    field({ id: "knowledgeContent", label: "正文", value: item.content || "", rows: 18, wide: true })
  );

  const enabledWrap = document.createElement("label");
  enabledWrap.className = "switch-card";
  enabledWrap.innerHTML = `<div><div class="switch-title">启用条目</div><div class="switch-note">停用后不会进入 AI 上下文</div></div>`;
  const toggle = document.createElement("span");
  toggle.className = "toggle";
  toggle.innerHTML = `<input id="knowledgeEnabled" type="checkbox" ${item.enabled === 0 ? "" : "checked"}><span></span>`;
  enabledWrap.appendChild(toggle);

  const actions = document.createElement("div");
  actions.className = "danger-row";
  actions.appendChild(button("button", "重建 STS2 知识库", seedKnowledge));
  if (state.selectedKnowledgeId !== "new") {
    actions.appendChild(button("button danger", "删除条目", deleteKnowledge));
  }
  panel.append(grid, enabledWrap, actions);
}

function parseJsonArray(value) {
  if (Array.isArray(value)) return value;
  try {
    const parsed = JSON.parse(value || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return String(value || "").split(/[,，、\s]+/).filter(Boolean);
  }
}

function empty(text) {
  const node = document.createElement("div");
  node.className = "empty";
  node.textContent = text;
  return node;
}

function shortTime(value) {
  const text = String(value || "").trim();
  if (!text) return "无记录";
  return text.replace("T", " ").slice(0, 19);
}

function messageTypeText(message) {
  const types = Array.isArray(message.segment_types) ? message.segment_types.filter(Boolean) : [];
  if (types.length) return types.slice(0, 4).join(" / ");
  return message.message_type || "message";
}

function resetArchiveView() {
  state.archiveExpanded = false;
  state.archiveFilterUserId = "";
  state.archiveRows = [];
  state.archiveHasMore = false;
  state.archiveOffset = 0;
}

function memberById(userId) {
  const id = String(userId || "");
  return (state.group?.members || []).find((item) => String(item.user_id || "") === id) || null;
}

function archiveDisplay(message) {
  const member = memberById(message.user_id) || {};
  return {
    displayName: member.display_name || message.display_name || message.sender_name || message.user_id || "未知成员",
    title: member.title || "",
    avatarUrl: member.avatar_url || "",
  };
}

function renderGroups() {
  const group = state.group;
  setTitle("群管理", group ? `当前群：${state.selectedGroupId}` : "选择左侧群聊后管理功能开关。");
  const content = $("content");
  content.innerHTML = "";
  if (!group) {
    content.appendChild(empty("左侧选择一个群。"));
    return;
  }

  const layout = document.createElement("div");
  layout.className = "group-layout";

  const featuresPanel = document.createElement("div");
  featuresPanel.className = "panel section";
  const title = document.createElement("h2");
  title.className = "section-title";
  title.textContent = "群功能开关";

  const grid = document.createElement("div");
  grid.className = "switch-grid";
  grid.append(
    switchCard("ai_chat", "AI 对话", "允许群内触发猎宝回复"),
    dailyReportCard(),
    switchCard("companion", "智能陪伴", "启用群友画像和记忆"),
    chimeCard()
  );

  featuresPanel.append(title, grid);
  layout.appendChild(featuresPanel);

  if (group.features?.companion) {
    layout.appendChild(renderCompanionFeaturePanel());
    layout.appendChild(renderGroupProfilePanel());
    layout.appendChild(renderCompanionManager());
  } else {
    const disabled = document.createElement("div");
    disabled.className = "empty";
    disabled.textContent = "开启智能陪伴后可管理本群群友画像。";
    layout.appendChild(disabled);
  }
  layout.appendChild(renderArchivePanel());

  content.appendChild(layout);
}

function renderCompanionFeaturePanel() {
  const panel = document.createElement("div");
  panel.className = "panel section";

  const title = document.createElement("h2");
  title.className = "section-title";
  title.textContent = "智能陪伴附加功能";

  const grid = document.createElement("div");
  grid.className = "companion-feature-grid";
  grid.append(
    featureLimitCard(
      "bot_tease",
      "调戏其他bot",
      "仅在已标记为 bot 的群友发言后触发，按限额主动接一句。"
    ),
    featureLimitCard(
      "constant_retort",
      "🎒常数回怼",
      "检测消息文本、卡片文本或已展开聊天记录中出现数字 158 时发送表情包。"
    )
  );

  panel.append(title, grid);
  return panel;
}

function featureLimitCard(key, title, note) {
  const wrap = document.createElement("div");
  wrap.className = "switch-card limit-card";

  const header = document.createElement("label");
  header.className = "limit-card-header";
  const text = document.createElement("div");
  text.innerHTML = `<div class="switch-title"></div><div class="switch-note"></div>`;
  text.children[0].textContent = title;
  text.children[1].textContent = note;

  const toggle = document.createElement("span");
  toggle.className = "toggle";
  toggle.innerHTML = `<input id="feature_${key}" type="checkbox" ${state.group?.features?.[key] ? "checked" : ""}><span></span>`;
  toggle.querySelector("input").onchange = () => saveGroup({ auto: true }).catch((error) => status(error.message, true));
  header.append(text, toggle);

  const limits = state.group?.limits?.[key] || {};
  const usage = state.group?.usage?.[key] || {};
  const limitGrid = document.createElement("div");
  limitGrid.className = "limit-grid";
  const limitItems = [
    ["per_minute", "每分钟", "已使用 本分钟"],
    ["per_hour", "每小时", "每小时"],
    ["per_day", "每天", "每天"],
  ];
  for (const item of limitItems) {
    const id = `limit_${key}_${item[0]}`;
    const wrapField = document.createElement("label");
    wrapField.className = "number-field";
    wrapField.innerHTML = `<span>${item[1]}</span><input id="${id}" type="number" min="0" max="999" step="1">`;
    const input = wrapField.querySelector("input");
    input.value = String(Number(limits[item[0]] ?? 0));
    input.onchange = () => saveGroup({ auto: true }).catch((error) => status(error.message, true));
    limitGrid.appendChild(wrapField);
  }

  const usageGrid = document.createElement("div");
  usageGrid.className = "limit-usage-grid";
  for (const item of limitItems) {
    const itemUsage = document.createElement("div");
    itemUsage.className = "limit-usage";
    itemUsage.innerHTML = `<span></span><strong></strong><span>次</span>`;
    itemUsage.children[0].textContent = item[2];
    itemUsage.querySelector("strong").textContent = String(Number(usage[item[0]] ?? 0));
    usageGrid.appendChild(itemUsage);
  }

  wrap.append(header, limitGrid, usageGrid);
  return wrap;
}

function renderGroupProfilePanel() {
  const profile = state.group?.group_profile || {};
  const panel = document.createElement("div");
  panel.className = "panel section group-profile-panel";

  const title = document.createElement("h2");
  title.className = "section-title";
  title.textContent = "群画像";

  const grid = document.createElement("div");
  grid.className = "form-grid";
  grid.append(
    field({
      id: "groupProfileSummary",
      label: "群性质与回复参考",
      value: profile.summary || "",
      rows: 4,
      wide: true,
    }),
    field({
      id: "groupProfileMaxChars",
      label: "字数上限",
      value: profile.max_chars || 100,
      type: "number",
    })
  );

  const meta = document.createElement("p");
  meta.className = "muted";
  meta.textContent = profile.updated_at ? `上次保存：${shortTime(profile.updated_at)}` : "尚未保存群画像。";

  panel.append(title, grid, meta);
  return panel;
}

function switchCard(key, title, note) {
  const wrap = document.createElement("label");
  wrap.className = "switch-card";
  wrap.innerHTML = `<div><div class="switch-title">${title}</div><div class="switch-note">${note}</div></div>`;
  const toggle = document.createElement("span");
  toggle.className = "toggle";
  const checked = state.group?.features?.[key] ? "checked" : "";
  toggle.innerHTML = `<input id="feature_${key}" type="checkbox" ${checked}><span></span>`;
  toggle.querySelector("input").onchange = () => saveGroup({ auto: true }).catch((error) => status(error.message, true));
  wrap.appendChild(toggle);
  return wrap;
}

function dailyReportCard() {
  const reportEnabled = Boolean(state.group?.features?.daily_report);
  const autoEnabled = reportEnabled && Boolean(state.group?.features?.daily_report_auto);
  const wrap = document.createElement("div");
  wrap.className = "switch-card daily-report-card";

  const main = document.createElement("label");
  main.className = "daily-report-row";
  main.innerHTML = `<div><div class="switch-title">日报</div><div class="switch-note">同时控制消息采集和日报数据</div></div>`;
  const reportToggle = document.createElement("span");
  reportToggle.className = "toggle";
  reportToggle.innerHTML = `<input id="feature_daily_report" type="checkbox" ${reportEnabled ? "checked" : ""}><span></span>`;
  reportToggle.querySelector("input").onchange = () => saveGroup({ auto: true }).catch((error) => status(error.message, true));
  main.appendChild(reportToggle);

  const auto = document.createElement("label");
  auto.className = `daily-report-row daily-report-auto${reportEnabled ? "" : " disabled"}`;
  auto.innerHTML = `<div><div class="switch-title">自动发送日报</div><div class="switch-note">定时生成后私聊发送给管理员</div></div>`;
  const autoToggle = document.createElement("span");
  autoToggle.className = "toggle";
  autoToggle.innerHTML = `<input id="feature_daily_report_auto" type="checkbox" ${autoEnabled ? "checked" : ""} ${reportEnabled ? "" : "disabled"}><span></span>`;
  autoToggle.querySelector("input").onchange = () => saveGroup({ auto: true }).catch((error) => status(error.message, true));
  auto.appendChild(autoToggle);

  wrap.append(main, auto);
  return wrap;
}

function chimeCard() {
  const wrap = document.createElement("div");
  wrap.className = "switch-card chime-card";

  const text = document.createElement("div");
  const title = document.createElement("div");
  title.className = "switch-title";
  title.textContent = "🎒常数报时";
  const note = document.createElement("div");
  note.className = "switch-note";

  const modeWrap = document.createElement("div");
  modeWrap.className = "segmented chime-state";

  const enabled = Boolean(state.group?.features?.hourly_chime);
  const currentMode = state.group?.chime?.mode || "hourly";
  const currentValue = enabled ? currentMode : "off";
  for (const item of [
    { value: "off", label: "关闭" },
    { value: "hourly", label: "每小时报时" },
    { value: "twice_daily", label: "每天两次" },
  ]) {
    const option = document.createElement("label");
    option.className = `segmented-option${currentValue === item.value ? " active" : ""}`;
    option.innerHTML = `<input type="radio" name="chimeState" value="${item.value}" ${currentValue === item.value ? "checked" : ""}><span>${item.label}</span>`;
    option.querySelector("input").onchange = () => saveGroup({ auto: true }).catch((error) => status(error.message, true));
    modeWrap.appendChild(option);
  }

  note.textContent = !enabled
    ? "当前关闭"
    : currentMode === "twice_daily"
    ? "凌晨1:58和下午1:58"
    : "每小时58分";
  text.append(title, note);
  wrap.append(text, modeWrap);
  return wrap;
}

function selectedChimeState() {
  return document.querySelector('input[name="chimeState"]:checked')?.value || (
    state.group?.features?.hourly_chime ? (state.group?.chime?.mode || "hourly") : "off"
  );
}

function selectedChimeEnabled() {
  return selectedChimeState() !== "off";
}

function selectedChimeMode() {
  const selected = selectedChimeState();
  return selected === "off" ? (state.group?.chime?.mode || "hourly") : selected;
}

function renderCompanionManager() {
  const panel = document.createElement("div");
  panel.className = "panel section";

  const title = document.createElement("h2");
  title.className = "section-title";
  title.textContent = "群友画像管理";

  const layout = document.createElement("div");
  layout.id = "companionLayout";
  layout.className = "companion-layout";

  const left = document.createElement("div");
  left.id = "companionMemberColumn";
  left.className = "companion-member-column";
  const search = document.createElement("input");
  search.className = "search";
  search.placeholder = "搜索昵称或 QQ";
  search.value = state.companionSearch;
  search.oninput = () => {
    state.companionSearch = search.value;
    state.companionListScrollTop = 0;
    render();
  };
  const members = filteredMembers();
  const selectedVisible = selectedVisibleMemberIds();
  const allVisibleSelected = members.length > 0 && selectedVisible.length === members.length;
  const bulk = document.createElement("div");
  bulk.className = "member-bulk-toolbar";

  const selectAll = document.createElement("label");
  selectAll.className = "member-bulk-select";
  selectAll.innerHTML = `<input id="memberBulkSelectAll" type="checkbox" ${allVisibleSelected ? "checked" : ""}><span>全选</span>`;
  selectAll.querySelector("input").onchange = (event) => {
    if (event.target.checked) {
      for (const member of members) {
        if (member.user_id) state.selectedCompanionIds.add(String(member.user_id));
      }
    } else {
      for (const member of members) {
        if (member.user_id) state.selectedCompanionIds.delete(String(member.user_id));
      }
    }
    render();
  };

  const count = document.createElement("span");
  count.className = "member-bulk-count";
  count.textContent = `已选 ${selectedVisible.length}`;

  bulk.append(
    selectAll,
    count,
    button("button small", "清空", () => {
      state.selectedCompanionIds.clear();
      render();
    }),
    button("button small", "开启记录", () => bulkSetCompanionTargets(true)),
    button("button small", "关闭记录", () => bulkSetCompanionTargets(false))
  );

  const list = document.createElement("div");
  list.id = "memberChipList";
  list.className = "member-chip-list";

  if (!members.length) {
    list.appendChild(empty("未读取到群成员。"));
  } else {
    for (const member of members) {
      list.appendChild(memberChip(member));
    }
  }
  requestAnimationFrame(() => {
    const currentList = $("memberChipList");
    if (currentList) currentList.scrollTop = state.companionListScrollTop || 0;
  });
  left.append(search, bulk, list);

  const right = document.createElement("div");
  right.id = "companionEditor";
  right.appendChild(renderCompanionEditor());

  layout.append(left, right);
  panel.append(title, layout);
  return panel;
}

function filteredMembers() {
  const members = state.group?.members || [];
  const query = state.companionSearch.trim().toLowerCase();
  const matched = query
    ? members.filter((item) => {
      const haystack = `${item.display_name || ""} ${item.nickname || ""} ${item.card || ""} ${item.title || ""} ${item.user_id || ""}`.toLowerCase();
      return haystack.includes(query);
    })
    : members.slice();
  return matched.sort((left, right) => {
    const leftEnabled = left.target_enabled ? 1 : 0;
    const rightEnabled = right.target_enabled ? 1 : 0;
    if (leftEnabled !== rightEnabled) return rightEnabled - leftEnabled;
    return String(left.display_name || left.user_id || "").localeCompare(
      String(right.display_name || right.user_id || ""),
      "zh-Hans-CN",
      { numeric: true, sensitivity: "base" }
    );
  });
}

function memberChip(member) {
  const node = document.createElement("button");
  const userId = String(member.user_id || "");
  node.className = `member-chip${state.selectedCompanionId === member.user_id ? " active" : ""}${member.target_enabled ? " enabled" : ""}${state.selectedCompanionIds.has(userId) ? " selected" : ""}`;
  node.type = "button";
  node.onclick = async () => {
    const list = $("memberChipList");
    if (list) state.companionListScrollTop = list.scrollTop;
    state.selectedCompanionId = member.user_id;
    state.companionDetail = await api(`${API}/api/groups/${state.selectedGroupId}/companions/${member.user_id}`);
    render();
  };

  const avatar = document.createElement("img");
  avatar.className = "member-avatar";
  avatar.src = member.avatar_url || "";
  avatar.alt = "";
  avatar.loading = "lazy";
  avatar.decoding = "async";
  avatar.onerror = () => avatar.classList.add("missing");

  const check = document.createElement("span");
  check.className = "member-select";
  check.innerHTML = `<input type="checkbox" ${state.selectedCompanionIds.has(userId) ? "checked" : ""} aria-label="选择群友"><span></span>`;
  const checkbox = check.querySelector("input");
  checkbox.onclick = (event) => event.stopPropagation();
  checkbox.onchange = (event) => {
    event.stopPropagation();
    if (event.target.checked) {
      state.selectedCompanionIds.add(userId);
    } else {
      state.selectedCompanionIds.delete(userId);
    }
    render();
  };

  const body = document.createElement("div");
  body.className = "member-body";

  const line = document.createElement("div");
  line.className = "member-line";
  const name = document.createElement("span");
  name.className = "member-name";
  name.textContent = member.display_name || member.user_id || "未知成员";
  line.appendChild(name);
  if (member.title) {
    const title = document.createElement("span");
    title.className = "member-title";
    title.textContent = member.title;
    line.appendChild(title);
  }

  const qq = document.createElement("div");
  qq.className = "member-qq";
  qq.textContent = member.user_id || "";

  body.append(line, qq);

  const mark = document.createElement("span");
  mark.className = "member-mark";
  mark.textContent = member.target_enabled ? "已记录" : "未记录";

  node.append(check, avatar, body, mark);
  return node;
}

function selectedMember() {
  return (state.group?.members || []).find((item) => item.user_id === state.selectedCompanionId) || null;
}

function renderCompanionEditor() {
  if (!state.companionDetail) {
    return empty("选择左侧群友查看画像。");
  }
  const target = state.companionDetail.target || {};
  const member = state.companionDetail.member || selectedMember() || {};
  const profile = state.companionDetail.profile || {};
  const panel = document.createElement("div");

  const detailHeader = document.createElement("div");
  detailHeader.className = "companion-detail-header";
  const avatar = document.createElement("img");
  avatar.className = "companion-detail-avatar";
  avatar.src = member.avatar_url || selectedMember()?.avatar_url || "";
  avatar.alt = "";
  avatar.loading = "lazy";
  avatar.decoding = "async";
  avatar.onerror = () => avatar.classList.add("missing");

  const titleBlock = document.createElement("div");
  titleBlock.className = "companion-detail-title";
  const nameLine = document.createElement("div");
  nameLine.className = "companion-detail-name-line";
  const name = document.createElement("strong");
  name.textContent = member.display_name || target.display_name || state.selectedCompanionId;
  nameLine.appendChild(name);
  if (member.title) {
    const title = document.createElement("span");
    title.className = "member-title";
    title.textContent = member.title;
    nameLine.appendChild(title);
  }
  const qq = document.createElement("div");
  qq.className = "companion-detail-qq";
  qq.textContent = `QQ ${state.selectedCompanionId}`;
  titleBlock.append(nameLine, qq);
  detailHeader.append(avatar, titleBlock);

  const targetWrap = document.createElement("label");
  targetWrap.className = "switch-card companion-target-switch";
  targetWrap.innerHTML = `<div><div class="switch-title">允许记录画像</div><div class="switch-note">仅在当前群内对这个 QQ 生效</div></div>`;
  const targetToggle = document.createElement("span");
  targetToggle.className = "toggle";
  targetToggle.innerHTML = `<input id="companionTargetEnabled" type="checkbox" ${target.enabled || member.target_enabled ? "checked" : ""}><span></span>`;
  targetToggle.querySelector("input").onchange = () => saveCompanionTarget({ auto: true }).catch((error) => status(error.message, true));
  targetWrap.appendChild(targetToggle);

  const botWrap = document.createElement("label");
  botWrap.className = "switch-card companion-target-switch";
  botWrap.innerHTML = `<div><div class="switch-title">标记为其他bot</div><div class="switch-note">用于“调戏其他bot”，仅当前群生效</div></div>`;
  const botToggle = document.createElement("span");
  botToggle.className = "toggle";
  botToggle.innerHTML = `<input id="companionIsBot" type="checkbox" ${target.is_bot || member.is_bot ? "checked" : ""}><span></span>`;
  botToggle.querySelector("input").onchange = () => saveCompanionTarget({ auto: true }).catch((error) => status(error.message, true));
  botWrap.appendChild(botToggle);

  const grid = document.createElement("div");
  grid.className = "form-grid";
  grid.append(
    field({ id: "profileCurrent", label: "近期在做", value: profile.current_activity || "", rows: 4 }),
    field({ id: "profileStyle", label: "互动风格", value: profile.personality_notes || "", rows: 4 }),
    field({ id: "profilePreference", label: "陪伴偏好", value: profile.emotional_preferences || "", rows: 4 }),
    field({ id: "profileTopics", label: "常聊主题", value: parseJsonArray(profile.topics).join("，"), rows: 4 }),
    field({
      id: "botKeywords",
      label: "bot关键词",
      value: parseJsonArray(target.bot_keywords || member.bot_keywords).join("，"),
      rows: 3,
    }),
    field({ id: "profileSummary", label: "画像摘要", value: profile.summary || "", rows: 6, wide: true }),
    field({ id: "profileConfidence", label: "置信度 0-1", value: profile.confidence ?? "", type: "number" })
  );

  const actions = document.createElement("div");
  actions.className = "danger-row";
  actions.append(
    button("button", "保存记录状态", saveCompanionTarget),
    button("button", "重置画像", resetCompanion),
    button("button danger", "删除画像", deleteCompanion)
  );

  panel.append(detailHeader, targetWrap, botWrap, grid, actions);
  return panel;
}

function renderArchivePanel() {
  const archive = state.group?.archive || {};
  const panel = document.createElement("div");
  panel.className = "panel section";

  const title = document.createElement("h2");
  title.className = "section-title";
  title.textContent = "消息采集记录";

  const summary = document.createElement("div");
  summary.className = "archive-summary";
  const enabled = document.createElement("span");
  enabled.className = `status-pill ${archive.enabled ? "on" : "off"}`;
  enabled.textContent = archive.enabled ? "日报开启" : "日报关闭";
  const count = document.createElement("span");
  count.className = "stat-pill";
  count.textContent = `已采集 ${Number(archive.message_count || 0)} 条`;
  const latest = document.createElement("span");
  latest.className = "stat-pill";
  latest.textContent = `最近 ${shortTime(archive.last_message_at)}`;
  summary.append(enabled, count, latest);

  const controls = document.createElement("div");
  controls.className = "archive-controls";
  controls.appendChild(button("button", state.archiveExpanded ? "收起记录" : "展开记录", () => {
    if (state.archiveExpanded) {
      resetArchiveView();
      render();
      return;
    }
    state.archiveExpanded = true;
    loadArchivePage(true)
      .then(() => render())
      .catch((error) => status(error.message, true));
  }));

  if (state.archiveExpanded) {
    const filter = document.createElement("select");
    filter.className = "archive-filter";
    filter.value = state.archiveFilterUserId;
    const allOption = document.createElement("option");
    allOption.value = "";
    allOption.textContent = "全部 QQ";
    filter.appendChild(allOption);
    for (const member of state.group?.members || []) {
      const option = document.createElement("option");
      option.value = member.user_id || "";
      option.textContent = `${member.display_name || member.user_id} / ${member.user_id}`;
      filter.appendChild(option);
    }
    filter.onchange = () => {
      state.archiveFilterUserId = filter.value;
      loadArchivePage(true)
        .then(() => render())
        .catch((error) => status(error.message, true));
    };
    controls.appendChild(filter);
  }

  const messages = state.archiveExpanded
    ? state.archiveRows
    : (Array.isArray(archive.recent_messages) ? archive.recent_messages : []);
  const list = document.createElement("div");
  list.className = "archive-list";
  if (!messages.length) {
    list.appendChild(empty(state.archiveExpanded ? "没有符合条件的采集记录。" : "暂无采集记录。"));
  } else {
    for (const item of messages) {
      list.appendChild(archiveMessageRow(item));
    }
  }

  if (state.archiveExpanded && state.archiveHasMore) {
    const more = button("button ghost archive-more", "加载更多", () => {
      loadArchivePage(false)
        .then(() => render())
        .catch((error) => status(error.message, true));
    });
    list.appendChild(more);
  }

  panel.append(title, summary, controls, list);
  return panel;
}

function archiveMessageRow(item) {
  const row = document.createElement("div");
  row.className = "archive-item";
  const display = archiveDisplay(item);

  const avatar = document.createElement("img");
  avatar.className = "archive-avatar";
  avatar.src = display.avatarUrl || "";
  avatar.alt = "";
  avatar.loading = "lazy";
  avatar.decoding = "async";
  avatar.onerror = () => avatar.classList.add("missing");

  const body = document.createElement("div");
  body.className = "archive-message-body";

  const person = document.createElement("div");
  person.className = "archive-person-line";
  const name = document.createElement("span");
  name.className = "archive-name";
  name.textContent = display.displayName;
  person.appendChild(name);
  if (display.title) {
    const title = document.createElement("span");
    title.className = "member-title";
    title.textContent = display.title;
    person.appendChild(title);
  }

  const meta = document.createElement("div");
  meta.className = "archive-meta";
  meta.textContent = `QQ ${item.user_id || ""}`;

  const preview = document.createElement("div");
  preview.className = "archive-preview";
  preview.textContent = item.preview_text || item.plain_text || "[非文本消息]";

  const detail = document.createElement("div");
  detail.className = "archive-detail";
  detail.textContent = `${shortTime(item.created_at)} / ${messageTypeText(item)}`;

  body.append(person, meta, preview, detail);
  row.append(avatar, body);
  return row;
}

function render() {
  renderSidePanel();
  if (state.view === "persona") renderPersona();
  if (state.view === "knowledge") renderKnowledge();
  if (state.view === "groups") renderGroups();
  $("saveButton").style.display = state.view ? "" : "none";
}

async function loadState() {
  status("正在读取...");
  state.data = await api(`${API}/api/state`);
  state.selectedGroupId = state.selectedGroupId || state.data.selected_group || "";
  state.group = state.data.group;
  $("version").textContent = state.data.version || "v1.2.0";
  status("已读取");
  render();
}

async function loadGroup(groupId) {
  if (!groupId) return;
  state.group = await api(`${API}/api/groups/${groupId}`);
  pruneSelectedCompanionIds();
}

async function loadArchivePage(reset = false) {
  if (!state.selectedGroupId) return;
  if (reset) {
    state.archiveRows = [];
    state.archiveOffset = 0;
    state.archiveHasMore = false;
  }
  const params = new URLSearchParams({
    limit: String(ARCHIVE_PAGE_SIZE),
    offset: String(state.archiveOffset),
  });
  if (state.archiveFilterUserId) {
    params.set("user_id", state.archiveFilterUserId);
  }
  const archive = await api(`${API}/api/groups/${state.selectedGroupId}/archive?${params.toString()}`);
  const rows = Array.isArray(archive.recent_messages) ? archive.recent_messages : [];
  state.archiveRows = reset ? rows : state.archiveRows.concat(rows);
  state.archiveOffset = state.archiveRows.length;
  state.archiveHasMore = Boolean(archive.has_more);
  if (state.group) {
    state.group.archive = archive;
  }
}

async function savePersona() {
  const result = await api(`${API}/api/persona`, {
    method: "PUT",
    body: JSON.stringify({ persona: readInput("personaText") }),
  });
  state.data.persona = result.persona;
  status("人设已保存");
}

async function saveKnowledge() {
  if (!state.knowledgeDetail) {
    status("先选择知识条目", true);
    return;
  }
  const payload = {
    title: readInput("knowledgeTitle"),
    category: readInput("knowledgeCategory"),
    keywords: parseKeywordInput(readInput("knowledgeKeywords")),
    content: readInput("knowledgeContent"),
    enabled: $("knowledgeEnabled")?.checked ?? true,
  };
  const path = state.selectedKnowledgeId === "new"
    ? `${API}/api/knowledge`
    : `${API}/api/knowledge/${state.selectedKnowledgeId}`;
  const result = await api(path, {
    method: state.selectedKnowledgeId === "new" ? "POST" : "PUT",
    body: JSON.stringify(payload),
  });
  state.selectedKnowledgeId = result.item.id;
  state.knowledgeDetail = result;
  state.data.knowledge = await api(`${API}/api/knowledge`);
  status("知识已保存");
  render();
}

async function deleteKnowledge() {
  if (!state.selectedKnowledgeId || state.selectedKnowledgeId === "new") return;
  if (!confirm("确定删除这条知识？")) return;
  const result = await api(`${API}/api/knowledge/${state.selectedKnowledgeId}`, { method: "DELETE" });
  state.data.knowledge = result.knowledge;
  state.selectedKnowledgeId = null;
  state.knowledgeDetail = null;
  status("知识已删除");
  render();
}

async function seedKnowledge() {
  if (!confirm("将重建 STS2 知识库：旧 STS1/STS2 条目会被清空后重新导入，非 STS 分类的手写知识不受影响。继续吗？")) return;
  status("正在重建 STS2 知识库...");
  const result = await api(`${API}/api/knowledge/seed-sts`, { method: "POST" });
  state.data.knowledge = result.knowledge;
  state.selectedKnowledgeId = null;
  state.knowledgeDetail = null;
  status(`已重建 ${result.result.inserted || 0} 条 STS2 知识`);
  render();
}

async function saveGroup(options = {}) {
  if (!state.selectedGroupId) {
    status("先选择群", true);
    return;
  }
  const payload = {
    features: {
      ai_chat: $("feature_ai_chat")?.checked ?? false,
      daily_report: $("feature_daily_report")?.checked ?? false,
      daily_report_auto: ($("feature_daily_report")?.checked ?? false) && ($("feature_daily_report_auto")?.checked ?? false),
      companion: $("feature_companion")?.checked ?? false,
      hourly_chime: selectedChimeEnabled(),
      bot_tease: $("feature_bot_tease")?.checked ?? false,
      constant_retort: $("feature_constant_retort")?.checked ?? false,
    },
    chime: {
      mode: selectedChimeMode(),
    },
    limits: {
      bot_tease: {
        per_minute: readNumber("limit_bot_tease_per_minute", state.group?.limits?.bot_tease?.per_minute || 1),
        per_hour: readNumber("limit_bot_tease_per_hour", state.group?.limits?.bot_tease?.per_hour || 3),
        per_day: readNumber("limit_bot_tease_per_day", state.group?.limits?.bot_tease?.per_day || 6),
      },
      constant_retort: {
        per_minute: readNumber("limit_constant_retort_per_minute", state.group?.limits?.constant_retort?.per_minute || 5),
        per_hour: readNumber("limit_constant_retort_per_hour", state.group?.limits?.constant_retort?.per_hour || 10),
        per_day: readNumber("limit_constant_retort_per_day", state.group?.limits?.constant_retort?.per_day || 15),
      },
    },
    group_profile: {
      summary: readInput("groupProfileSummary"),
      max_chars: readNumber("groupProfileMaxChars", state.group?.group_profile?.max_chars || 100),
    },
  };
  state.group = await api(`${API}/api/groups/${state.selectedGroupId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
  status(options.auto ? "群开关已更新" : "群设置已保存");
  render();
}

async function saveCompanion() {
  if (!state.selectedCompanionId) {
    status("先选择群友", true);
    return;
  }
  state.companionDetail = await api(`${API}/api/groups/${state.selectedGroupId}/companions/${state.selectedCompanionId}`, {
    method: "PUT",
    body: JSON.stringify({
      current_activity: readInput("profileCurrent"),
      personality_notes: readInput("profileStyle"),
      emotional_preferences: readInput("profilePreference"),
      topics: parseKeywordInput(readInput("profileTopics")),
      summary: readInput("profileSummary"),
      confidence: readInput("profileConfidence"),
    }),
  });
  state.group = await api(`${API}/api/groups/${state.selectedGroupId}`);
  status("画像已保存");
  render();
}

async function saveCompanionTarget(options = {}) {
  if (!state.selectedCompanionId) {
    status("先选择群友", true);
    return;
  }
  const member = selectedMember() || {};
  const list = $("memberChipList");
  if (list) state.companionListScrollTop = list.scrollTop;
  state.companionDetail = await api(`${API}/api/groups/${state.selectedGroupId}/companions/${state.selectedCompanionId}/target`, {
    method: "PUT",
    body: JSON.stringify({
      enabled: $("companionTargetEnabled")?.checked ?? false,
      display_name: member.display_name || "",
      is_bot: $("companionIsBot")?.checked ?? false,
      bot_keywords: parseKeywordInput(readInput("botKeywords")),
    }),
  });
  state.group = await api(`${API}/api/groups/${state.selectedGroupId}`);
  pruneSelectedCompanionIds();
  status(options.auto ? "记录状态已更新" : "记录状态已保存");
  render();
}

async function bulkSetCompanionTargets(enabled) {
  const userIds = selectedVisibleMemberIds();
  if (!userIds.length) {
    status("先选择群友", true);
    return;
  }
  const list = $("memberChipList");
  if (list) state.companionListScrollTop = list.scrollTop;
  status(enabled ? "正在批量开启记录..." : "正在批量关闭记录...");
  const result = await api(`${API}/api/groups/${state.selectedGroupId}/companions/targets/bulk`, {
    method: "PUT",
    body: JSON.stringify({
      user_ids: userIds,
      enabled,
    }),
  });
  state.group = result.group;
  pruneSelectedCompanionIds();
  if (state.selectedCompanionId) {
    state.companionDetail = await api(`${API}/api/groups/${state.selectedGroupId}/companions/${state.selectedCompanionId}`);
  }
  status(`${enabled ? "已开启" : "已关闭"} ${result.updated || userIds.length} 人画像记录`);
  render();
}

async function resetCompanion() {
  if (!state.selectedCompanionId) return;
  if (!confirm("确定重置这个群友画像？记录状态会保留。")) return;
  state.companionDetail = await api(`${API}/api/groups/${state.selectedGroupId}/companions/${state.selectedCompanionId}/reset`, {
    method: "POST",
  });
  state.group = await api(`${API}/api/groups/${state.selectedGroupId}`);
  status("画像已重置");
  render();
}

async function deleteCompanion() {
  if (!state.selectedCompanionId) return;
  if (!confirm("确定删除这个群友画像并关闭记录？")) return;
  const result = await api(`${API}/api/groups/${state.selectedGroupId}/companions/${state.selectedCompanionId}`, {
    method: "DELETE",
  });
  state.group = result.group;
  pruneSelectedCompanionIds();
  state.selectedCompanionId = null;
  state.companionDetail = null;
  status("画像已删除");
  render();
}

async function saveCurrent() {
  try {
    status("正在保存...");
    if (state.view === "persona") {
      await savePersona();
      return;
    }
    if (state.view === "knowledge") {
      await saveKnowledge();
      return;
    }
    if (state.view === "groups") {
      await saveGroup();
    }
  } catch (error) {
    status(error.message, true);
  }
}

document.querySelectorAll("[data-view]").forEach((node) => {
  node.addEventListener("click", () => setView(node.dataset.view));
});

applyTheme(preferredTheme());
document.querySelectorAll("[data-theme-option]").forEach((node) => {
  node.addEventListener("click", () => setTheme(node.dataset.themeOption));
});
$("refreshButton").onclick = () => loadState().catch((error) => status(error.message, true));
$("saveButton").onclick = saveCurrent;

loadState().catch((error) => status(error.message, true));
