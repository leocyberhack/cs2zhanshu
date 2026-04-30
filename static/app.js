const economyOptions = ["手枪局", "eco局", "半起局", "反eco局", "长枪局", "通用", "自定义"];
const tacticTagOptions = ["常规默认", "非常规", "爆弹", "rush", "自定义"];
const mapAccents = ["#1f8f74", "#df6a3f", "#337aa3", "#e4b548", "#8c6bb1", "#bc3f3a"];

const tabs = [
  { id: "tactics-T", label: "T 方战术", type: "tactics", side: "T" },
  { id: "tactics-CT", label: "CT 方战术", type: "tactics", side: "CT" },
  { id: "notes-T", label: "T 方注意事项和技巧", type: "notes", side: "T" },
  { id: "notes-CT", label: "CT 方注意事项和技巧", type: "notes", side: "CT" },
];

const state = {
  maps: [],
  selectedMapId: null,
  content: null,
  activeTab: "tactics-T",
  selectedTacticIds: { T: null, CT: null },
  tacticDrafts: { T: null, CT: null },
  mapModalMode: "create",
};

const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function uid(prefix) {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function unique(list) {
  return [...new Set(list.map((item) => String(item).trim()).filter(Boolean))];
}

function emptyDecisionNode() {
  return {
    id: uid("node"),
    condition: "",
    thenAction: "",
    children: [],
  };
}

function normalizeDecisionNode(node) {
  const source = node && typeof node === "object" ? node : {};
  const children = Array.isArray(source.children)
    ? source.children
    : Array.isArray(source.thenChildren)
      ? source.thenChildren
      : [];
  return {
    id: source.id || uid("node"),
    condition: source.condition || "",
    thenAction: source.thenAction || "",
    children: children.map((child) => normalizeDecisionNode(child)),
  };
}

function normalizeDecisionTree(tree) {
  if (Array.isArray(tree)) return tree.map((node) => normalizeDecisionNode(node));
  if (tree && typeof tree === "object") {
    if (Array.isArray(tree.nodes)) return tree.nodes.map((node) => normalizeDecisionNode(node));
    if (tree.condition || tree.thenAction || tree.children || tree.thenChildren) {
      return [normalizeDecisionNode(tree)];
    }
  }
  return [];
}

function emptyTactic(side) {
  return {
    id: `draft-${side}`,
    side,
    title: "",
    economy: "通用",
    economy_custom: "",
    tactic_tags: ["常规默认"],
    tactic_custom_tags: [],
    early_commands: [],
    decision_tree: [],
    updated_at: "",
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : {};
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login") {
      showLogin(data.error || "请先输入密钥登录");
    }
    throw new Error(data.error || "请求失败");
  }
  return data;
}

function toast(message, type = "normal") {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast ${type === "error" ? "error" : ""}`;
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => node.classList.add("hidden"), 2600);
}

function currentMap() {
  return state.maps.find((item) => item.id === state.selectedMapId) || null;
}

function currentTab() {
  return tabs.find((item) => item.id === state.activeTab) || tabs[0];
}

function sameId(left, right) {
  return String(left) === String(right);
}

function setLoginHint(message = "") {
  const hint = $("#loginHint");
  if (hint) hint.textContent = message;
}

function showLogin(message = "") {
  $("#appShell")?.classList.add("hidden");
  $("#authGate")?.classList.remove("hidden");
  setLoginHint(message);
  window.setTimeout(() => $("#accessKeyInput")?.focus(), 0);
}

function showApp() {
  $("#authGate")?.classList.add("hidden");
  $("#appShell")?.classList.remove("hidden");
  setLoginHint("");
}

async function checkAuth() {
  const data = await api("/api/auth");
  if (data.authenticated) {
    showApp();
    await loadMaps();
    return;
  }
  const message = data.configured ? "" : `服务器未配置 ${data.env}`;
  showLogin(message);
}

async function loginWithKey() {
  const input = $("#accessKeyInput");
  const key = input?.value.trim() || "";
  if (!key) {
    setLoginHint("请输入访问密钥");
    input?.focus();
    return;
  }
  await api("/api/login", {
    method: "POST",
    body: JSON.stringify({ key }),
  });
  if (input) input.value = "";
  showApp();
  await loadMaps();
}

async function logout() {
  await api("/api/logout", { method: "POST" });
  state.maps = [];
  state.selectedMapId = null;
  state.content = null;
  state.selectedTacticIds = { T: null, CT: null };
  state.tacticDrafts = { T: null, CT: null };
  showLogin("已退出登录");
}

function filenameFromDisposition(disposition, fallback) {
  const match = /filename="?([^"]+)"?/i.exec(disposition || "");
  return match ? match[1] : fallback;
}

async function downloadExport(format) {
  const response = await fetch(`/api/export?format=${encodeURIComponent(format)}`, {
    credentials: "same-origin",
  });
  if (!response.ok) {
    let message = "下载失败";
    try {
      const data = await response.json();
      message = data.error || message;
    } catch {}
    if (response.status === 401) showLogin(message);
    throw new Error(message);
  }
  const blob = await response.blob();
  const fallback = format === "pdf" ? "cs2-tactics-book.pdf" : "cs2-tactics-book.docx";
  const filename = filenameFromDisposition(response.headers.get("Content-Disposition"), fallback);
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function loadMaps(preferredId = state.selectedMapId) {
  const data = await api("/api/maps");
  state.maps = data.maps || [];
  if (!state.maps.length) {
    state.selectedMapId = null;
    state.content = null;
    renderAll();
    return;
  }
  const preferred = state.maps.find((item) => item.id === preferredId);
  state.selectedMapId = preferred ? preferred.id : state.maps[0].id;
  await loadContent();
}

async function loadContent() {
  if (!state.selectedMapId) {
    state.content = null;
    renderAll();
    return;
  }
  state.content = await api(`/api/maps/${state.selectedMapId}/content`);
  for (const side of ["T", "CT"]) {
    const list = state.content.tactics?.[side] || [];
    list.forEach((tactic) => {
      tactic.decision_tree = normalizeDecisionTree(tactic.decision_tree);
    });
    const selected = list.find((item) => sameId(item.id, state.selectedTacticIds[side]));
    if (!selected && state.selectedTacticIds[side] !== `draft-${side}`) {
      state.selectedTacticIds[side] = null;
    }
  }
  renderAll();
}

function renderAll() {
  renderMapList();
  renderTopbar();
  renderTabs();
  renderContent();
}

function renderMapList() {
  const list = $("#mapList");
  if (!state.maps.length) {
    list.innerHTML = `<div class="empty-state">地图池为空</div>`;
    return;
  }
  list.innerHTML = state.maps
    .map((map, index) => {
      const counts = map.counts || {};
      return `
        <button class="map-item ${map.id === state.selectedMapId ? "active" : ""}"
          type="button"
          data-action="select-map"
          data-id="${map.id}"
          style="--map-accent:${mapAccents[index % mapAccents.length]}33">
          <div class="map-title-row">
            <span class="map-title">${escapeHtml(map.name)}</span>
          </div>
          <div class="map-counts">
            <span>T 战术 ${counts.t_tactics || 0}</span>
            <span>CT 战术 ${counts.ct_tactics || 0}</span>
            <span>T 技巧 ${counts.t_notes || 0}</span>
            <span>CT 技巧 ${counts.ct_notes || 0}</span>
          </div>
        </button>
      `;
    })
    .join("");
}

function renderTopbar() {
  const map = currentMap();
  $("#currentMapName").textContent = map ? map.name : "未选择";
  document.querySelectorAll("[data-action='open-map-modal'][data-mode='edit'], [data-action='delete-map']")
    .forEach((button) => {
      button.disabled = !map;
    });
}

function tabCount(tab) {
  if (!state.content) return 0;
  if (tab.type === "tactics") return state.content.tactics?.[tab.side]?.length || 0;
  return state.content.notes?.[tab.side]?.length || 0;
}

function renderTabs() {
  $("#tabs").innerHTML = tabs
    .map((tab) => `
      <button class="tab ${tab.id === state.activeTab ? "active" : ""}"
        type="button"
        data-action="switch-tab"
        data-tab="${tab.id}">
        ${escapeHtml(tab.label)} · ${tabCount(tab)}
      </button>
    `)
    .join("");
}

function renderContent() {
  const content = $("#content");
  if (!state.maps.length) {
    content.innerHTML = `
      <div class="empty-state">
        <button class="btn primary" type="button" data-action="open-map-modal" data-mode="create">新增第一张地图</button>
      </div>
    `;
    return;
  }
  const tab = currentTab();
  if (tab.type === "tactics") {
    renderTactics(tab.side);
  } else {
    renderNotes(tab.side);
  }
}

function getTacticList(side) {
  return state.content?.tactics?.[side] || [];
}

function getCurrentTactic(side) {
  if (state.selectedTacticIds[side] === `draft-${side}`) {
    return state.tacticDrafts[side];
  }
  return getTacticList(side).find((item) => sameId(item.id, state.selectedTacticIds[side])) || null;
}

function tacticMeta(tactic) {
  const tags = [...(tactic.tactic_tags || []).filter((tag) => tag !== "自定义"), ...(tactic.tactic_custom_tags || [])];
  const economy = tactic.economy === "自定义" ? tactic.economy_custom : tactic.economy;
  return `${economy || "通用"} · ${tags.join(" / ") || "未标记"}`;
}

function renderTactics(side) {
  const list = getTacticList(side);
  const draft = state.selectedTacticIds[side] === `draft-${side}` ? state.tacticDrafts[side] : null;
  const selectedTactic = getCurrentTactic(side);
  const displayList = draft ? [draft, ...list] : list;
  $("#content").innerHTML = `
    <div class="tactic-layout">
      <aside class="panel list-panel">
        <div class="panel-head">
          <h3>${side === "T" ? "T 方战术" : "CT 方战术"}</h3>
          <button class="btn small primary" type="button" data-action="add-tactic" data-side="${side}">新增战术</button>
        </div>
        <div class="tactic-list">
          ${
            displayList.length
              ? displayList.map((tactic) => renderTacticCard(tactic, side)).join("")
              : `<div class="empty-state">暂无战术</div>`
          }
        </div>
      </aside>
      <section class="panel editor ${selectedTactic ? "has-selection" : "no-selection"}">
        ${renderTacticEditor(side)}
      </section>
    </div>
  `;
  syncEconomyCustom();
}

function renderTacticCard(tactic, side) {
  return `
    <button class="tactic-card ${sameId(state.selectedTacticIds[side], tactic.id) ? "active" : ""}"
      type="button"
      data-action="select-tactic"
      data-side="${side}"
      data-id="${tactic.id}">
      <h4>${escapeHtml(tactic.title || "新战术")}</h4>
      <div class="meta-line">${escapeHtml(tacticMeta(tactic))}</div>
      <div class="updated-line">${tactic.id === `draft-${side}` ? "草稿" : escapeHtml(tactic.updated_at || "")}</div>
    </button>
  `;
}

function renderTacticEditor(side) {
  const tactic = getCurrentTactic(side);
  if (!tactic) {
    return `<div class="empty-state">选择或新增一条战术</div>`;
  }
  tactic.decision_tree = normalizeDecisionTree(tactic.decision_tree);
  const tags = tactic.tactic_tags || [];
  return `
    <div class="editor-actions">
      <button class="btn primary" type="button" data-action="save-tactic" data-side="${side}">保存战术</button>
      <button class="btn danger" type="button" data-action="delete-tactic" data-side="${side}">删除战术</button>
    </div>

    <div class="section">
      <div class="editor-grid">
        <label class="field">
          <span>战术名称</span>
          <input id="tacticTitle" type="text" value="${escapeHtml(tactic.title)}" maxlength="100" />
        </label>
        <label class="field">
          <span>经济属性</span>
          <select id="economySelect">
            ${economyOptions.map((item) => `<option value="${escapeHtml(item)}" ${item === tactic.economy ? "selected" : ""}>${escapeHtml(item)}</option>`).join("")}
          </select>
        </label>
      </div>
      <label class="field ${tactic.economy === "自定义" ? "" : "hidden"}" id="economyCustomWrap">
        <span>自定义经济属性</span>
        <input id="economyCustom" type="text" value="${escapeHtml(tactic.economy_custom)}" maxlength="50" />
      </label>
    </div>

    <div class="section">
      <h3 class="section-title">战术属性</h3>
      <div class="chip-row">
        ${tacticTagOptions.map((item) => `
          <label class="chip">
            <input type="checkbox" name="tacticTag" value="${escapeHtml(item)}" ${tags.includes(item) ? "checked" : ""} />
            <span>${escapeHtml(item)}</span>
          </label>
        `).join("")}
      </div>
      <div class="chip-row" id="customTags">
        ${(tactic.tactic_custom_tags || []).map((tag) => renderCustomTag(tag)).join("")}
      </div>
      <div class="inline-add">
        <input id="customTagInput" type="text" placeholder="自定义战术属性" maxlength="40" />
        <button class="btn ghost" type="button" data-action="add-custom-tag" data-side="${side}">添加属性</button>
      </div>
    </div>

    <div class="section">
      <div class="panel-head">
        <h3 class="section-title">前期战术展开</h3>
        <button class="btn small ghost" type="button" data-action="add-command" data-side="${side}">新增命令</button>
      </div>
      <div class="command-list">
        ${renderCommandRows(tactic.early_commands || [])}
      </div>
      <div id="executionPreview">
        ${renderExecutionPreview(tactic.early_commands || [])}
      </div>
    </div>

    <div class="section">
      <div class="panel-head">
        <h3 class="section-title">中期决策</h3>
        <button class="btn small ghost" type="button" data-action="add-root-decision-node" data-side="${side}">新增并行判定</button>
      </div>
      ${renderDecisionTree(tactic.decision_tree)}
    </div>
  `;
}

function renderCustomTag(tag) {
  return `
    <span class="chip custom-tag" data-custom-tag="${escapeHtml(tag)}">
      ${escapeHtml(tag)}
      <button type="button" data-action="remove-custom-tag" data-tag="${escapeHtml(tag)}" aria-label="删除自定义属性">×</button>
    </span>
  `;
}

function renderCommandRows(commands) {
  if (!commands.length) {
    return `<div class="empty-state">暂无前期命令</div>`;
  }
  return commands
    .map((command) => `
      <div class="command-row" data-command-id="${escapeHtml(command.id)}">
        <input class="priority-input" type="number" min="1" step="1" value="${escapeHtml(command.priority)}" aria-label="优先级" />
        <input class="command-text" type="text" value="${escapeHtml(command.text)}" placeholder="子战术命令" />
        <button class="icon-btn" type="button" data-action="remove-command" data-id="${escapeHtml(command.id)}" aria-label="删除命令">×</button>
      </div>
    `)
    .join("");
}

function renderExecutionPreview(commands) {
  const clean = [...commands]
    .filter((item) => String(item.text || "").trim())
    .sort((a, b) => Number(a.priority || 1) - Number(b.priority || 1));
  if (!clean.length) {
    return `<div class="execution-preview"><span class="meta-line">暂无执行顺序</span></div>`;
  }
  const groups = clean.reduce((acc, item) => {
    const key = String(Math.max(1, Number(item.priority || 1)));
    acc[key] = acc[key] || [];
    acc[key].push(item.text);
    return acc;
  }, {});
  return `
    <div class="execution-preview">
      ${Object.keys(groups).sort((a, b) => Number(a) - Number(b)).map((priority) => `
        <div class="priority-group">
          <span class="priority-badge">P${priority}</span>
          <div class="priority-texts">
            ${groups[priority].map((text) => `<span>${escapeHtml(text)}</span>`).join("")}
          </div>
        </div>
      `).join("")}
    </div>
  `;
}

function decisionTitle(node, index, depth) {
  if (depth === 0) return `并行判定 ${index + 1}`;
  const label = node.condition ? `如果「${node.condition}」` : `下级判定 ${index + 1}`;
  return label;
}

function renderDecisionTree(nodes) {
  if (!nodes.length) {
    return `
      <div class="decision-tree">
        <div class="empty-state">暂无中期判断</div>
      </div>
    `;
  }
  return `
    <div class="decision-tree">
      <div class="decision-stack root-stack">
        ${nodes.map((node, index) => renderDecisionNode(node, `${index}`, 0, index)).join("")}
      </div>
    </div>
  `;
}

function renderDecisionNode(node, path, depth = 0, index = 0) {
  node.children = node.children || [];
  const title = decisionTitle(node, index, depth);
  return `
    <div class="decision-node ${depth === 0 ? "root-node" : "child"}" data-path="${escapeHtml(path)}" style="--depth:${depth}">
      <div class="decision-title">
        <span>${escapeHtml(title)}</span>
        <div class="node-tools">
          <button class="btn small ghost" type="button" data-action="add-child-decision-node" data-path="${escapeHtml(path)}">新增下级判定</button>
          <button class="btn small danger" type="button" data-action="remove-decision-node" data-path="${escapeHtml(path)}">删除</button>
        </div>
      </div>
      <div class="node-fields">
        <label class="field">
          <span>如果</span>
          <input type="text" value="${escapeHtml(node.condition)}" data-decision-field="condition" data-path="${escapeHtml(path)}" />
        </label>
        <label class="field">
          <span>那么</span>
          <textarea data-decision-field="thenAction" data-path="${escapeHtml(path)}">${escapeHtml(node.thenAction)}</textarea>
        </label>
      </div>
      ${renderDecisionChildren(node.children, `${path}.children`, depth + 1)}
    </div>
  `;
}

function renderDecisionChildren(children, branchPath, depth) {
  if (!children.length) return "";
  return `
    <div class="decision-children">
      <div class="branch-label">挂在上方“那么”之后</div>
      <div class="decision-stack">
        ${children.map((child, index) => renderDecisionNode(child, `${branchPath}.${index}`, depth, index)).join("")}
      </div>
    </div>
  `;
}

function renderNotes(side) {
  const notes = state.content?.notes?.[side] || [];
  $("#content").innerHTML = `
    <div class="notes-layout">
      <section class="notes-list">
        ${
          notes.length
            ? notes.map((note, index) => renderNoteCard(note, index)).join("")
            : `<div class="empty-state">暂无内容</div>`
        }
      </section>
      <aside class="panel new-note">
        <div class="section">
          <h3 class="section-title">${side === "T" ? "新增 T 方内容" : "新增 CT 方内容"}</h3>
          <label class="field">
            <span>内容</span>
            <textarea id="newNoteText"></textarea>
          </label>
          <button class="btn primary" type="button" data-action="add-note" data-side="${side}">保存</button>
        </div>
      </aside>
    </div>
  `;
}

function renderNoteCard(note, index) {
  return `
    <article class="note-card">
      <p class="note-index">${index + 1}</p>
      <div class="note-body">
        <textarea data-note-id="${note.id}">${escapeHtml(note.body)}</textarea>
        <div class="note-actions">
          <button class="btn small primary" type="button" data-action="save-note" data-id="${note.id}">保存</button>
          <button class="btn small danger" type="button" data-action="delete-note" data-id="${note.id}">删除</button>
          <span class="meta-line">${escapeHtml(note.updated_at || "")}</span>
        </div>
      </div>
    </article>
  `;
}

function hydrateCurrentTacticFromForm() {
  const tab = currentTab();
  if (tab.type !== "tactics") return;
  const tactic = getCurrentTactic(tab.side);
  if (!tactic || !$("#tacticTitle")) return;

  tactic.title = $("#tacticTitle").value.trim();
  tactic.economy = $("#economySelect").value;
  tactic.economy_custom = $("#economyCustom")?.value.trim() || "";
  tactic.tactic_tags = [...document.querySelectorAll("input[name='tacticTag']:checked")].map((item) => item.value);
  tactic.tactic_custom_tags = unique([...document.querySelectorAll("[data-custom-tag]")].map((item) => item.dataset.customTag));
  if (tactic.tactic_custom_tags.length && !tactic.tactic_tags.includes("自定义")) {
    tactic.tactic_tags.push("自定义");
  }
  tactic.early_commands = [...document.querySelectorAll(".command-row")].map((row) => ({
    id: row.dataset.commandId || uid("cmd"),
    priority: Math.max(1, Number(row.querySelector(".priority-input")?.value || 1)),
    text: row.querySelector(".command-text")?.value.trim() || "",
  }));
}

function getDecisionNode(path) {
  const tab = currentTab();
  const tactic = getCurrentTactic(tab.side);
  if (!tactic) return null;
  tactic.decision_tree = normalizeDecisionTree(tactic.decision_tree);
  const parts = path.split(".");
  let node = tactic.decision_tree[Number(parts[0])];
  for (let index = 1; index < parts.length; index += 2) {
    const childIndex = Number(parts[index + 1]);
    node = node?.children?.[childIndex];
    if (!node) return null;
  }
  return node;
}

function removeDecisionNode(path) {
  const tab = currentTab();
  const tactic = getCurrentTactic(tab.side);
  if (!tactic) return;
  tactic.decision_tree = normalizeDecisionTree(tactic.decision_tree);
  const parts = path.split(".");
  if (parts.length === 1) {
    tactic.decision_tree.splice(Number(parts[0]), 1);
    return;
  }
  const childIndex = Number(parts.at(-1));
  const parentPath = parts.slice(0, -2).join(".");
  const parent = getDecisionNode(parentPath);
  if (parent?.children) {
    parent.children.splice(childIndex, 1);
  }
}

function syncEconomyCustom() {
  const wrap = $("#economyCustomWrap");
  const select = $("#economySelect");
  if (!wrap || !select) return;
  wrap.classList.toggle("hidden", select.value !== "自定义");
}

async function saveMap() {
  const name = $("#mapNameInput").value.trim();
  if (!name) {
    toast("地图名称不能为空", "error");
    return;
  }
  if (state.mapModalMode === "edit") {
    const map = currentMap();
    if (!map) return;
    const data = await api(`/api/maps/${map.id}`, {
      method: "PUT",
      body: JSON.stringify({ name }),
    });
    state.selectedMapId = data.map.id;
    toast("地图已更新");
  } else {
    const data = await api("/api/maps", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    state.selectedMapId = data.map.id;
    toast("地图已创建");
  }
  closeMapModal();
  await loadMaps(state.selectedMapId);
}

function openMapModal(mode) {
  const map = currentMap();
  if (mode === "edit" && !map) return;
  state.mapModalMode = mode;
  $("#mapModalTitle").textContent = mode === "edit" ? "重命名地图" : "新增地图";
  $("#mapNameInput").value = mode === "edit" ? map.name : "";
  $("#mapModal").classList.remove("hidden");
  setTimeout(() => $("#mapNameInput").focus(), 0);
}

function closeMapModal() {
  $("#mapModal").classList.add("hidden");
}

async function saveTactic(side) {
  hydrateCurrentTacticFromForm();
  const tactic = getCurrentTactic(side);
  if (!tactic) return;
  const payload = {
    map_id: state.selectedMapId,
    side,
    title: tactic.title,
    economy: tactic.economy,
    economy_custom: tactic.economy_custom,
    tactic_tags: tactic.tactic_tags,
    tactic_custom_tags: tactic.tactic_custom_tags,
    early_commands: tactic.early_commands,
    decision_tree: normalizeDecisionTree(tactic.decision_tree),
  };
  const isDraft = tactic.id === `draft-${side}`;
  const data = await api(isDraft ? "/api/tactics" : `/api/tactics/${tactic.id}`, {
    method: isDraft ? "POST" : "PUT",
    body: JSON.stringify(payload),
  });
  state.selectedTacticIds[side] = data.tactic.id;
  state.tacticDrafts[side] = null;
  toast("战术已保存");
  await loadMaps(state.selectedMapId);
}

async function deleteTactic(side) {
  const tactic = getCurrentTactic(side);
  if (!tactic) return;
  if (tactic.id === `draft-${side}`) {
    state.tacticDrafts[side] = null;
    state.selectedTacticIds[side] = null;
    renderContent();
    return;
  }
  if (!confirm(`删除战术「${tactic.title || "未命名"}」？`)) return;
  await api(`/api/tactics/${tactic.id}`, { method: "DELETE" });
  state.selectedTacticIds[side] = null;
  toast("战术已删除");
  await loadMaps(state.selectedMapId);
}

async function addNote(side) {
  const body = $("#newNoteText")?.value.trim();
  if (!body) {
    toast("内容不能为空", "error");
    return;
  }
  await api("/api/notes", {
    method: "POST",
    body: JSON.stringify({ map_id: state.selectedMapId, side, body }),
  });
  toast("内容已保存");
  await loadMaps(state.selectedMapId);
}

async function saveNote(id) {
  const body = document.querySelector(`[data-note-id='${id}']`)?.value.trim();
  if (!body) {
    toast("内容不能为空", "error");
    return;
  }
  await api(`/api/notes/${id}`, {
    method: "PUT",
    body: JSON.stringify({ body }),
  });
  toast("内容已更新");
  await loadMaps(state.selectedMapId);
}

async function deleteNote(id) {
  if (!confirm("删除这条内容？")) return;
  await api(`/api/notes/${id}`, { method: "DELETE" });
  toast("内容已删除");
  await loadMaps(state.selectedMapId);
}

document.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;
  try {
    if (action === "logout") {
      await logout();
      return;
    }

    if (action === "download-export") {
      await downloadExport(target.dataset.format || "docx");
      toast("导出已开始");
      return;
    }

    if (action === "select-map") {
      hydrateCurrentTacticFromForm();
      state.selectedMapId = Number(target.dataset.id);
      state.tacticDrafts = { T: null, CT: null };
      await loadContent();
    }

    if (action === "switch-tab") {
      hydrateCurrentTacticFromForm();
      state.activeTab = target.dataset.tab;
      renderTabs();
      renderContent();
    }

    if (action === "open-map-modal") openMapModal(target.dataset.mode || "create");
    if (action === "close-map-modal") closeMapModal();
    if (action === "save-map") await saveMap();

    if (action === "delete-map") {
      const map = currentMap();
      if (!map || !confirm(`删除地图「${map.name}」及其全部内容？`)) return;
      await api(`/api/maps/${map.id}`, { method: "DELETE" });
      state.selectedMapId = null;
      toast("地图已删除");
      await loadMaps();
    }

    if (action === "add-tactic") {
      const side = target.dataset.side;
      hydrateCurrentTacticFromForm();
      state.tacticDrafts[side] = emptyTactic(side);
      state.selectedTacticIds[side] = `draft-${side}`;
      renderContent();
    }

    if (action === "select-tactic") {
      const side = target.dataset.side;
      hydrateCurrentTacticFromForm();
      state.selectedTacticIds[side] = target.dataset.id;
      renderContent();
    }

    if (action === "save-tactic") await saveTactic(target.dataset.side);
    if (action === "delete-tactic") await deleteTactic(target.dataset.side);

    if (action === "add-command") {
      const tactic = getCurrentTactic(target.dataset.side);
      hydrateCurrentTacticFromForm();
      tactic.early_commands.push({ id: uid("cmd"), priority: 1, text: "" });
      renderContent();
    }

    if (action === "remove-command") {
      const tab = currentTab();
      const tactic = getCurrentTactic(tab.side);
      hydrateCurrentTacticFromForm();
      tactic.early_commands = tactic.early_commands.filter((item) => item.id !== target.dataset.id);
      renderContent();
    }

    if (action === "add-custom-tag") {
      const tactic = getCurrentTactic(target.dataset.side);
      hydrateCurrentTacticFromForm();
      const value = $("#customTagInput")?.value.trim();
      if (value) {
        tactic.tactic_custom_tags = unique([...(tactic.tactic_custom_tags || []), value]);
        if (!tactic.tactic_tags.includes("自定义")) tactic.tactic_tags.push("自定义");
      }
      renderContent();
    }

    if (action === "remove-custom-tag") {
      const tab = currentTab();
      const tactic = getCurrentTactic(tab.side);
      hydrateCurrentTacticFromForm();
      tactic.tactic_custom_tags = tactic.tactic_custom_tags.filter((item) => item !== target.dataset.tag);
      renderContent();
    }

    if (action === "add-root-decision-node") {
      hydrateCurrentTacticFromForm();
      const tactic = getCurrentTactic(target.dataset.side);
      if (!tactic) return;
      tactic.decision_tree = normalizeDecisionTree(tactic.decision_tree);
      tactic.decision_tree.push(emptyDecisionNode());
      renderContent();
    }

    if (action === "add-child-decision-node") {
      hydrateCurrentTacticFromForm();
      const node = getDecisionNode(target.dataset.path);
      if (!node) return;
      node.children = node.children || [];
      node.children.push(emptyDecisionNode());
      renderContent();
    }

    if (action === "remove-decision-node") {
      hydrateCurrentTacticFromForm();
      removeDecisionNode(target.dataset.path);
      renderContent();
    }

    if (action === "add-note") await addNote(target.dataset.side);
    if (action === "save-note") await saveNote(target.dataset.id);
    if (action === "delete-note") await deleteNote(target.dataset.id);
  } catch (error) {
    toast(error.message, "error");
  }
});

$("#loginForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    setLoginHint("");
    await loginWithKey();
  } catch (error) {
    setLoginHint(error.message);
  }
});

document.addEventListener("input", (event) => {
  const target = event.target;
  if (target.matches("[data-decision-field]")) {
    const node = getDecisionNode(target.dataset.path);
    if (node) node[target.dataset.decisionField] = target.value;
  }
  if (target.matches(".priority-input, .command-text")) {
    hydrateCurrentTacticFromForm();
    $("#executionPreview").innerHTML = renderExecutionPreview(getCurrentTactic(currentTab().side)?.early_commands || []);
  }
});

document.addEventListener("change", (event) => {
  if (event.target.matches("#economySelect")) {
    syncEconomyCustom();
  }
});

document.addEventListener("keydown", async (event) => {
  if ($("#mapModal").classList.contains("hidden")) return;
  if (event.key === "Escape") closeMapModal();
  if (event.key === "Enter" && event.target.matches("#mapNameInput")) {
    try {
      await saveMap();
    } catch (error) {
      toast(error.message, "error");
    }
  }
});

checkAuth().catch((error) => showLogin(error.message));
