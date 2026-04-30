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
let executionPreviewFrame = 0;
let contentRequestId = 0;

const baselines = {
  tactics: { T: new Map(), CT: new Map() },
  notes: { T: new Map(), CT: new Map() },
};

const ACTIONS_WITH_PENDING = new Set([
  "logout",
  "download-export",
  "save-map",
  "delete-map",
  "save-tactic",
  "delete-tactic",
  "add-note",
  "save-note",
  "delete-note",
]);

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
  const headers = { ...(options.headers || {}) };
  if (options.body) {
    headers["Content-Type"] = headers["Content-Type"] || "application/json";
  }
  const response = await fetch(path, {
    ...options,
    credentials: "same-origin",
    headers,
  });
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { /* non-JSON response */ }
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

function emptyCounts() {
  return {
    t_tactics: 0,
    ct_tactics: 0,
    t_notes: 0,
    ct_notes: 0,
  };
}

function normalizeMap(map) {
  return {
    ...map,
    counts: {
      ...emptyCounts(),
      ...(map?.counts || {}),
    },
  };
}

function sortMaps(list) {
  return list.sort((left, right) => {
    const byOrder = Number(left.sort_order || 0) - Number(right.sort_order || 0);
    if (byOrder) return byOrder;
    return Number(left.id || 0) - Number(right.id || 0);
  });
}

function normalizeMaps(maps) {
  return sortMaps((maps || []).map((map) => normalizeMap(map)));
}

function emptyContentForMap(map) {
  return {
    map: normalizeMap(map),
    tactics: { T: [], CT: [] },
    notes: { T: [], CT: [] },
  };
}

function stableJson(value) {
  return JSON.stringify(value);
}

function decisionSnapshot(nodes) {
  return normalizeDecisionTree(nodes).map((node) => ({
    condition: String(node.condition || "").trim(),
    thenAction: String(node.thenAction || "").trim(),
    children: decisionSnapshot(node.children || []),
  }));
}

function tacticSnapshot(tactic) {
  return {
    title: String(tactic?.title || "").trim(),
    economy: tactic?.economy || "通用",
    economy_custom: String(tactic?.economy_custom || "").trim(),
    tactic_tags: unique(tactic?.tactic_tags || []),
    tactic_custom_tags: unique(tactic?.tactic_custom_tags || []),
    early_commands: (tactic?.early_commands || [])
      .map((command) => ({
        priority: Math.max(1, Number(command.priority || 1)),
        text: String(command.text || "").trim(),
      }))
      .filter((command) => command.text),
    decision_tree: decisionSnapshot(tactic?.decision_tree || []),
  };
}

function noteSnapshot(note) {
  return {
    body: String(note?.body || "").trim(),
  };
}

function hasMeaningfulDecision(nodes) {
  return decisionSnapshot(nodes).some((node) =>
    node.condition || node.thenAction || hasMeaningfulDecision(node.children || []),
  );
}

function hasMeaningfulTacticDraft(tactic) {
  const snapshot = tacticSnapshot(tactic);
  return Boolean(
    snapshot.title ||
      snapshot.economy !== "通用" ||
      snapshot.economy_custom ||
      snapshot.tactic_custom_tags.length ||
      snapshot.early_commands.length ||
      hasMeaningfulDecision(tactic?.decision_tree || []),
  );
}

function setTacticBaseline(tactic) {
  if (!tactic?.side || String(tactic.id).startsWith("draft-")) return;
  baselines.tactics[tactic.side].set(String(tactic.id), stableJson(tacticSnapshot(tactic)));
}

function removeTacticBaseline(side, id) {
  baselines.tactics[side]?.delete(String(id));
}

function setNoteBaseline(note) {
  if (!note?.side) return;
  baselines.notes[note.side].set(String(note.id), stableJson(noteSnapshot(note)));
}

function removeNoteBaseline(side, id) {
  baselines.notes[side]?.delete(String(id));
}

function resetBaselines() {
  for (const side of ["T", "CT"]) {
    baselines.tactics[side].clear();
    baselines.notes[side].clear();
    (state.content?.tactics?.[side] || []).forEach(setTacticBaseline);
    (state.content?.notes?.[side] || []).forEach(setNoteBaseline);
  }
}

function normalizeContent(content) {
  if (!content) return null;
  return {
    ...content,
    map: normalizeMap(content.map),
    tactics: {
      T: sortTactics((content.tactics?.T || []).map((tactic) => ({
        ...tactic,
        decision_tree: normalizeDecisionTree(tactic.decision_tree),
      }))),
      CT: sortTactics((content.tactics?.CT || []).map((tactic) => ({
        ...tactic,
        decision_tree: normalizeDecisionTree(tactic.decision_tree),
      }))),
    },
    notes: {
      T: sortNotes([...(content.notes?.T || [])]),
      CT: sortNotes([...(content.notes?.CT || [])]),
    },
  };
}

function applyContent(content) {
  state.content = normalizeContent(content);
  for (const side of ["T", "CT"]) {
    const list = state.content?.tactics?.[side] || [];
    const selected = list.find((item) => sameId(item.id, state.selectedTacticIds[side]));
    if (!selected && state.selectedTacticIds[side] !== `draft-${side}`) {
      state.selectedTacticIds[side] = null;
    }
  }
  restoreDraftsForCurrentContent();
  resetBaselines();
}

function restoreDraftsForCurrentContent() {
  const mapId = state.content?.map?.id;
  if (!mapId) return;
  for (const side of ["T", "CT"]) {
    if (state.tacticDrafts[side]) continue;
    const restored = restoreDraftFromStorage(mapId, side);
    if (!restored) {
      if (state.selectedTacticIds[side] === `draft-${side}`) {
        state.selectedTacticIds[side] = null;
      }
      continue;
    }
    state.tacticDrafts[side] = restored;
    const selectedId = state.selectedTacticIds[side];
    const selectedExists = selectedId && (
      sameId(selectedId, restored.id) ||
      (state.content?.tactics?.[side] || []).some((item) => sameId(item.id, selectedId))
    );
    if (!selectedExists) {
      state.selectedTacticIds[side] = restored.id;
    }
  }
}

function upsertMap(map) {
  const normalized = normalizeMap(map);
  const index = state.maps.findIndex((item) => sameId(item.id, normalized.id));
  if (index >= 0) {
    state.maps[index] = normalized;
  } else {
    state.maps.push(normalized);
  }
  sortMaps(state.maps);
  if (state.content?.map && sameId(state.content.map.id, normalized.id)) {
    state.content.map = normalizeMap({ ...state.content.map, ...normalized });
  }
  return normalized;
}

function removeMapFromState(id) {
  const index = state.maps.findIndex((item) => sameId(item.id, id));
  if (index < 0) return null;
  state.maps.splice(index, 1);
  return state.maps[Math.min(index, state.maps.length - 1)] || null;
}

function tacticSnapshotFromRenderedForm(side, tactic) {
  const tab = currentTab();
  if (tab.type !== "tactics" || tab.side !== side || !$("#tacticTitle")) {
    return tacticSnapshot(tactic);
  }
  return tacticSnapshot({
    ...tactic,
    title: $("#tacticTitle")?.value || "",
    economy: $("#economySelect")?.value || "通用",
    economy_custom: $("#economyCustom")?.value || "",
    tactic_tags: [...document.querySelectorAll("input[name='tacticTag']:checked")].map((item) => item.value),
    tactic_custom_tags: [...document.querySelectorAll("[data-custom-tag]")].map((item) => item.dataset.customTag),
    early_commands: [...document.querySelectorAll(".command-row")].map((row) => ({
      priority: row.querySelector(".priority-input")?.value || 1,
      text: row.querySelector(".command-text")?.value || "",
    })),
  });
}

function hasUnsavedTacticChanges() {
  for (const side of ["T", "CT"]) {
    const selectedId = state.selectedTacticIds[side];
    const tactics = state.content?.tactics?.[side] || [];
    for (const tactic of tactics) {
      const snapshot = sameId(tactic.id, selectedId)
        ? tacticSnapshotFromRenderedForm(side, tactic)
        : tacticSnapshot(tactic);
      const baseline = baselines.tactics[side].get(String(tactic.id));
      if (baseline && stableJson(snapshot) !== baseline) return true;
    }

    const draft = state.tacticDrafts[side];
    if (!draft) continue;
    const draftSnapshot = sameId(selectedId, draft.id)
      ? tacticSnapshotFromRenderedForm(side, draft)
      : tacticSnapshot(draft);
    if (hasMeaningfulTacticDraft(draftSnapshot)) return true;
  }
  return false;
}

function hasUnsavedNoteChanges() {
  const tab = currentTab();
  if (tab.type !== "notes") return false;
  if (($("#newNoteText")?.value || "").trim()) return true;
  for (const textarea of document.querySelectorAll("[data-note-id]")) {
    const side = tab.side;
    const baseline = baselines.notes[side].get(String(textarea.dataset.noteId));
    if (baseline && stableJson({ body: textarea.value.trim() }) !== baseline) return true;
  }
  return false;
}

function hasUnsavedChanges() {
  return hasUnsavedTacticChanges() || hasUnsavedNoteChanges();
}

function confirmDiscardUnsavedChanges() {
  if (!hasUnsavedChanges()) return true;
  return confirm("有未保存内容，离开后会丢失。继续吗？");
}

function clearDraftSelections(clearStorage = false) {
  const mapId = state.selectedMapId;
  for (const side of ["T", "CT"]) {
    if (clearStorage && mapId) {
      clearDraftStorage(mapId, side);
    }
    state.tacticDrafts[side] = null;
    if (state.selectedTacticIds[side] === `draft-${side}`) {
      state.selectedTacticIds[side] = null;
    }
  }
}

function setButtonPending(button, pending, label = "") {
  if (!button || !("disabled" in button)) return;
  if (pending) {
    button.dataset.originalText = button.dataset.originalText || button.textContent;
    if (label) button.textContent = label;
    button.disabled = true;
    button.dataset.pending = "true";
    return;
  }
  if (button.dataset.originalText) {
    button.textContent = button.dataset.originalText;
  }
  delete button.dataset.originalText;
  delete button.dataset.pending;
  button.disabled = false;
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
  const data = await loadBootstrap();
  if (data.authenticated) {
    showApp();
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
  await loadBootstrap();
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

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function downloadExport(format) {
  toast("正在创建导出任务...");
  const created = await api("/api/export-jobs", {
    method: "POST",
    body: JSON.stringify({ format }),
  });
  const job = await waitForExportJob(created.job?.id);
  toast("正在下载文件...");
  const response = await fetch(`/api/export-jobs/${encodeURIComponent(job.id)}/download`, {
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
  triggerDownload(blob, format, response);
}

async function waitForExportJob(jobId) {
  if (!jobId) throw new Error("导出任务创建失败");
  const startedAt = Date.now();
  let interval = 600;
  while (true) {
    if (Date.now() - startedAt > 180000) {
      throw new Error("导出超时，请稍后重试");
    }
    const data = await api(`/api/export-jobs/${encodeURIComponent(jobId)}`);
    const job = data.job || {};
    if (job.status === "done") return job;
    if (job.status === "error") throw new Error(job.error || "导出失败");
    toast(job.status === "running" ? "正在生成文件..." : "导出任务排队中...");
    await sleep(interval);
    interval = Math.min(1800, interval + 200);
  }
}

function triggerDownload(blob, format, response) {
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
  state.maps = normalizeMaps(data.maps || []);
  if (!state.maps.length) {
    state.selectedMapId = null;
    applyContent(null);
    renderAll();
    return;
  }
  const preferred = state.maps.find((item) => item.id === preferredId);
  state.selectedMapId = preferred ? preferred.id : state.maps[0].id;
  await loadContent();
}

async function loadBootstrap(preferredId = state.selectedMapId) {
  const query = preferredId ? `?map_id=${encodeURIComponent(preferredId)}` : "";
  const data = await api(`/api/bootstrap${query}`);
  if (!data.authenticated) return data;
  state.maps = normalizeMaps(data.maps || []);
  state.selectedMapId = data.selected_map_id || state.maps[0]?.id || null;
  applyContent(data.content || null);
  renderAll();
  return data;
}

async function loadContent() {
  const requestId = ++contentRequestId;
  const mapId = state.selectedMapId;
  if (!state.selectedMapId) {
    applyContent(null);
    renderAll();
    return;
  }
  const content = await api(`/api/maps/${mapId}/content`);
  if (requestId !== contentRequestId || !sameId(mapId, state.selectedMapId)) return;
  applyContent(content);
  renderAll();
}

function renderAll() {
  renderMapList();
  renderTopbar();
  renderTabs();
  renderContent();
}

const emptyStateIcon = `<svg width="48" height="48" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="6" y="10" width="36" height="28" rx="4" stroke="currentColor" stroke-width="2" fill="none"/><path d="M6 18h36" stroke="currentColor" stroke-width="2"/><circle cx="14" cy="14" r="2" fill="currentColor"/><circle cx="22" cy="14" r="2" fill="currentColor"/></svg>`;

function renderMapList() {
  const list = $("#mapList");
  if (!state.maps.length) {
    list.innerHTML = `<div class="empty-state">${emptyStateIcon}<p>地图池为空</p></div>`;
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
          draggable="true"
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

function countKey(type, side) {
  return `${side.toLowerCase()}_${type}`;
}

function adjustCurrentMapCount(type, side, delta) {
  const key = countKey(type, side);
  const update = (map) => {
    if (!map) return;
    map.counts = map.counts || {};
    map.counts[key] = Math.max(0, Number(map.counts[key] || 0) + delta);
  };
  update(currentMap());
  if (state.content?.map && sameId(state.content.map.id, state.selectedMapId)) {
    update(state.content.map);
  }
}

function ensureContentList(type, side) {
  if (!state.content) return [];
  state.content[type] = state.content[type] || { T: [], CT: [] };
  state.content[type][side] = state.content[type][side] || [];
  return state.content[type][side];
}

function sortTactics(list) {
  return list.sort((left, right) => {
    const byTime = String(right.updated_at || "").localeCompare(String(left.updated_at || ""));
    if (byTime) return byTime;
    return Number(right.id || 0) - Number(left.id || 0);
  });
}

function sortNotes(list) {
  return list.sort((left, right) => {
    const byOrder = Number(left.sort_order || 0) - Number(right.sort_order || 0);
    if (byOrder) return byOrder;
    return Number(left.id || 0) - Number(right.id || 0);
  });
}

function upsertTactic(tactic) {
  if (!state.content || !sameId(tactic.map_id, state.selectedMapId)) return false;
  const normalized = {
    ...tactic,
    decision_tree: normalizeDecisionTree(tactic.decision_tree),
  };
  const list = ensureContentList("tactics", normalized.side);
  const index = list.findIndex((item) => sameId(item.id, normalized.id));
  if (index >= 0) {
    list[index] = normalized;
    sortTactics(list);
    setTacticBaseline(normalized);
    return false;
  }
  list.push(normalized);
  sortTactics(list);
  setTacticBaseline(normalized);
  return true;
}

function removeTacticFromState(side, id) {
  const list = ensureContentList("tactics", side);
  const index = list.findIndex((item) => sameId(item.id, id));
  if (index < 0) return false;
  list.splice(index, 1);
  removeTacticBaseline(side, id);
  return true;
}

function upsertNote(note) {
  if (!state.content || !sameId(note.map_id, state.selectedMapId)) return false;
  const list = ensureContentList("notes", note.side);
  const index = list.findIndex((item) => sameId(item.id, note.id));
  if (index >= 0) {
    list[index] = note;
    sortNotes(list);
    setNoteBaseline(note);
    return false;
  }
  list.push(note);
  sortNotes(list);
  setNoteBaseline(note);
  return true;
}

function findNoteSide(id) {
  for (const side of ["T", "CT"]) {
    const list = ensureContentList("notes", side);
    if (list.some((item) => sameId(item.id, id))) return side;
  }
  return null;
}

function removeNoteFromState(id) {
  for (const side of ["T", "CT"]) {
    const list = ensureContentList("notes", side);
    const index = list.findIndex((item) => sameId(item.id, id));
    if (index >= 0) {
      list.splice(index, 1);
      removeNoteBaseline(side, id);
      return side;
    }
  }
  return null;
}

function renderTabs() {
  const container = $("#tabs");
  const existing = container.querySelectorAll(".tab");
  if (existing.length === tabs.length) {
    // Lightweight update: only change text + active class
    tabs.forEach((tab, i) => {
      const btn = existing[i];
      const label = `${escapeHtml(tab.label)} · ${tabCount(tab)}`;
      if (btn.innerHTML.trim() !== label.trim()) btn.innerHTML = label;
      btn.classList.toggle("active", tab.id === state.activeTab);
    });
    return;
  }
  container.innerHTML = tabs
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
        ${emptyStateIcon}
        <p>开始构建你的战术体系</p>
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
              : `<div class="empty-state">${emptyStateIcon}<p>暂无战术</p></div>`
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

function tacticDisplayList(side) {
  const list = getTacticList(side);
  const draft = state.selectedTacticIds[side] === `draft-${side}` ? state.tacticDrafts[side] : null;
  return draft ? [draft, ...list] : list;
}

function isActiveTacticSide(side) {
  const tab = currentTab();
  return tab.type === "tactics" && tab.side === side;
}

function renderTacticListOnly(side) {
  if (!isActiveTacticSide(side)) return;
  const container = document.querySelector(".tactic-list");
  if (!container) return;
  const displayList = tacticDisplayList(side);
  container.innerHTML = displayList.length
    ? displayList.map((tactic) => renderTacticCard(tactic, side)).join("")
    : `<div class="empty-state">${emptyStateIcon}<p>暂无战术</p></div>`;
}

function renderTacticEditorOnly(side) {
  if (!isActiveTacticSide(side)) return;
  const editor = document.querySelector(".editor");
  if (!editor) return;
  const selectedTactic = getCurrentTactic(side);
  editor.className = `panel editor ${selectedTactic ? "has-selection" : "no-selection"}`;
  editor.innerHTML = renderTacticEditor(side);
  syncEconomyCustom();
}

function renderTacticViewParts(side, options = {}) {
  const { list = true, editor = true, counts = false } = options;
  if (counts) {
    renderMapList();
    renderTabs();
  }
  if (list) renderTacticListOnly(side);
  if (editor) renderTacticEditorOnly(side);
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
    return `<div class="empty-state">${emptyStateIcon}<p>选择或新增一条战术</p></div>`;
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

function refreshExecutionPreview() {
  const preview = $("#executionPreview");
  if (!preview) return;
  const tactic = getCurrentTactic(currentTab().side);
  preview.innerHTML = renderExecutionPreview(tactic?.early_commands || []);
}

function scheduleExecutionPreviewRefresh() {
  if (executionPreviewFrame) return;
  const schedule = window.requestAnimationFrame || ((callback) => window.setTimeout(callback, 16));
  executionPreviewFrame = schedule(() => {
    executionPreviewFrame = 0;
    hydrateCurrentTacticFromForm();
    refreshExecutionPreview();
  });
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
            : `<div class="empty-state">${emptyStateIcon}<p>暂无内容</p></div>`
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

function renderNoteListOnly(side) {
  const tab = currentTab();
  if (tab.type !== "notes" || tab.side !== side) return;
  const container = document.querySelector(".notes-list");
  if (!container) return;
  const notes = state.content?.notes?.[side] || [];
  container.innerHTML = notes.length
    ? notes.map((note, index) => renderNoteCard(note, index)).join("")
    : `<div class="empty-state">${emptyStateIcon}<p>暂无内容</p></div>`;
}

function renderNoteViewParts(side, options = {}) {
  const { list = true, counts = false, clearDraft = false } = options;
  if (counts) {
    renderMapList();
    renderTabs();
  }
  if (list) renderNoteListOnly(side);
  if (clearDraft) {
    const input = $("#newNoteText");
    if (input) input.value = "";
  }
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

function hydrateDecisionNodesFromForm(nodes) {
  if (!nodes || !nodes.length) return;
  for (const el of document.querySelectorAll("[data-decision-field]")) {
    const targetNode = getDecisionNode(el.dataset.path);
    if (targetNode) {
      targetNode[el.dataset.decisionField] = el.value;
    }
  }
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
  // Sync decision tree fields from DOM
  hydrateDecisionNodesFromForm(tactic.decision_tree);
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
  let needsFullRender = false;
  if (state.mapModalMode === "edit") {
    const map = currentMap();
    if (!map) return;
    const data = await api(`/api/maps/${map.id}`, {
      method: "PUT",
      body: JSON.stringify({ name }),
    });
    const updatedMap = upsertMap(data.map);
    state.selectedMapId = updatedMap.id;
    toast("地图已更新");
  } else {
    const data = await api("/api/maps", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    const createdMap = upsertMap(data.map);
    state.selectedMapId = createdMap.id;
    clearDraftSelections();
    applyContent(emptyContentForMap(createdMap));
    needsFullRender = true;
    toast("地图已创建");
  }
  closeMapModal();
  if (needsFullRender) {
    renderAll();
  } else {
    renderMapList();
    renderTopbar();
  }
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
  upsertTactic(data.tactic);
  if (isDraft) adjustCurrentMapCount("tactics", side, 1);
  state.selectedTacticIds[side] = data.tactic.id;
  state.tacticDrafts[side] = null;
  toast("战术已保存");
  renderTacticViewParts(side, { counts: isDraft });
}

async function deleteTactic(side) {
  const tactic = getCurrentTactic(side);
  if (!tactic) return;
  if (tactic.id === `draft-${side}`) {
    state.tacticDrafts[side] = null;
    state.selectedTacticIds[side] = null;
    renderTacticViewParts(side);
    return;
  }
  if (!confirm(`删除战术「${tactic.title || "未命名"}」？`)) return;
  await api(`/api/tactics/${tactic.id}`, { method: "DELETE" });
  if (removeTacticFromState(side, tactic.id)) {
    adjustCurrentMapCount("tactics", side, -1);
  }
  state.selectedTacticIds[side] = null;
  toast("战术已删除");
  renderTacticViewParts(side, { counts: true });
}

async function addNote(side) {
  const body = $("#newNoteText")?.value.trim();
  if (!body) {
    toast("内容不能为空", "error");
    return;
  }
  const data = await api("/api/notes", {
    method: "POST",
    body: JSON.stringify({ map_id: state.selectedMapId, side, body }),
  });
  upsertNote(data.note);
  adjustCurrentMapCount("notes", side, 1);
  toast("内容已保存");
  renderNoteViewParts(side, { counts: true, clearDraft: true });
}

async function saveNote(id) {
  const textarea = document.querySelector(`[data-note-id="${CSS.escape(String(id))}"]`);
  const body = textarea?.value.trim();
  if (!body) {
    toast("内容不能为空", "error");
    return;
  }
  const data = await api(`/api/notes/${id}`, {
    method: "PUT",
    body: JSON.stringify({ body }),
  });
  upsertNote(data.note);
  toast("内容已更新");
  renderNoteViewParts(data.note.side);
}

async function deleteNote(id) {
  if (!confirm("删除这条内容？")) return;
  const side = findNoteSide(id);
  await api(`/api/notes/${id}`, { method: "DELETE" });
  if (side && removeNoteFromState(id)) adjustCurrentMapCount("notes", side, -1);
  toast("内容已删除");
  if (side) {
    renderNoteViewParts(side, { counts: true });
  } else {
    renderTabs();
    renderMapList();
  }
}

document.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;
  const pendingAction = ACTIONS_WITH_PENDING.has(action);
  if (pendingAction && target.dataset.pending === "true") return;
  if (pendingAction) {
    setButtonPending(target, true, action === "download-export" ? "生成中..." : "");
  }
  try {
    if (action === "logout") {
      const dirty = hasUnsavedChanges();
      if (!confirmDiscardUnsavedChanges()) return;
      if (dirty) clearDraftSelections(true);
      await logout();
      return;
    }

    if (action === "download-export") {
      await downloadExport(target.dataset.format || "docx");
      toast("导出已下载");
      return;
    }

    if (action === "select-map") {
      const nextMapId = Number(target.dataset.id);
      if (sameId(nextMapId, state.selectedMapId)) return;
      const dirty = hasUnsavedChanges();
      if (!confirmDiscardUnsavedChanges()) return;
      if (!dirty) hydrateCurrentTacticFromForm();
      clearDraftSelections(dirty);
      state.selectedMapId = nextMapId;
      state.tacticDrafts = { T: null, CT: null };
      await loadContent();
      return;
    }

    if (action === "switch-tab") {
      const nextTab = target.dataset.tab;
      if (nextTab === state.activeTab) return;
      const dirty = hasUnsavedChanges();
      if (dirty) {
        if (!confirmDiscardUnsavedChanges()) return;
        clearDraftSelections(true);
        state.activeTab = nextTab;
        await loadContent();
        return;
      }
      hydrateCurrentTacticFromForm();
      state.activeTab = nextTab;
      renderTabs();
      renderContent();
      return;
    }

    if (action === "open-map-modal") {
      openMapModal(target.dataset.mode || "create");
      return;
    }
    if (action === "close-map-modal") {
      closeMapModal();
      return;
    }
    if (action === "save-map") {
      await saveMap();
      return;
    }

    if (action === "delete-map") {
      if (!confirmDiscardUnsavedChanges()) return;
      const map = currentMap();
      if (!map || !confirm(`删除地图「${map.name}」及其全部内容？`)) return;
      await api(`/api/maps/${map.id}`, { method: "DELETE" });
      clearDraftSelections(true);
      const nextMap = removeMapFromState(map.id);
      state.selectedMapId = nextMap?.id || null;
      applyContent(null);
      toast("地图已删除");
      if (nextMap) {
        await loadContent();
      } else {
        renderAll();
      }
      return;
    }

    if (action === "add-tactic") {
      const side = target.dataset.side;
      hydrateCurrentTacticFromForm();
      state.tacticDrafts[side] = emptyTactic(side);
      state.selectedTacticIds[side] = `draft-${side}`;
      renderTacticViewParts(side);
      return;
    }

    if (action === "select-tactic") {
      const side = target.dataset.side;
      hydrateCurrentTacticFromForm();
      const editorPanel = document.querySelector(".editor");
      const scrollY = editorPanel?.scrollTop || 0;
      state.selectedTacticIds[side] = target.dataset.id;
      renderTacticViewParts(side);
      const newEditor = document.querySelector(".editor");
      if (newEditor) newEditor.scrollTop = scrollY;
      return;
    }

    if (action === "save-tactic") {
      await saveTactic(target.dataset.side);
      return;
    }
    if (action === "delete-tactic") {
      await deleteTactic(target.dataset.side);
      return;
    }

    if (action === "add-command") {
      hydrateCurrentTacticFromForm();
      const tactic = getCurrentTactic(target.dataset.side);
      if (!tactic) return;
      tactic.early_commands.push({ id: uid("cmd"), priority: 1, text: "" });
      renderTacticViewParts(target.dataset.side);
      return;
    }

    if (action === "remove-command") {
      const tab = currentTab();
      const tactic = getCurrentTactic(tab.side);
      hydrateCurrentTacticFromForm();
      tactic.early_commands = tactic.early_commands.filter((item) => item.id !== target.dataset.id);
      renderTacticViewParts(tab.side);
      return;
    }

    if (action === "add-custom-tag") {
      const tactic = getCurrentTactic(target.dataset.side);
      hydrateCurrentTacticFromForm();
      const value = $("#customTagInput")?.value.trim();
      if (value) {
        tactic.tactic_custom_tags = unique([...(tactic.tactic_custom_tags || []), value]);
        if (!tactic.tactic_tags.includes("自定义")) tactic.tactic_tags.push("自定义");
      }
      renderTacticViewParts(target.dataset.side);
      return;
    }

    if (action === "remove-custom-tag") {
      const tab = currentTab();
      const tactic = getCurrentTactic(tab.side);
      hydrateCurrentTacticFromForm();
      tactic.tactic_custom_tags = tactic.tactic_custom_tags.filter((item) => item !== target.dataset.tag);
      renderTacticViewParts(tab.side);
      return;
    }

    if (action === "add-root-decision-node") {
      hydrateCurrentTacticFromForm();
      const tactic = getCurrentTactic(target.dataset.side);
      if (!tactic) return;
      tactic.decision_tree = normalizeDecisionTree(tactic.decision_tree);
      tactic.decision_tree.push(emptyDecisionNode());
      renderTacticViewParts(target.dataset.side);
      return;
    }

    if (action === "add-child-decision-node") {
      hydrateCurrentTacticFromForm();
      const node = getDecisionNode(target.dataset.path);
      if (!node) return;
      node.children = node.children || [];
      node.children.push(emptyDecisionNode());
      renderTacticViewParts(currentTab().side);
      return;
    }

    if (action === "remove-decision-node") {
      hydrateCurrentTacticFromForm();
      removeDecisionNode(target.dataset.path);
      renderTacticViewParts(currentTab().side);
      return;
    }

    if (action === "add-note") {
      await addNote(target.dataset.side);
      return;
    }
    if (action === "save-note") {
      await saveNote(target.dataset.id);
      return;
    }
    if (action === "delete-note") {
      await deleteNote(target.dataset.id);
      return;
    }
  } catch (error) {
    toast(error.message, "error");
  } finally {
    if (pendingAction && target.isConnected) {
      setButtonPending(target, false);
    }
  }
});

$("#loginForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  if (button?.dataset.pending === "true") return;
  setButtonPending(button, true, "登录中...");
  try {
    setLoginHint("");
    await loginWithKey();
  } catch (error) {
    setLoginHint(error.message);
  } finally {
    if (button?.isConnected) setButtonPending(button, false);
  }
});

document.addEventListener("input", (event) => {
  const target = event.target;
  if (target.matches("[data-decision-field]")) {
    const node = getDecisionNode(target.dataset.path);
    if (node) node[target.dataset.decisionField] = target.value;
  }
  if (target.matches(".priority-input, .command-text")) {
    scheduleExecutionPreviewRefresh();
  }
});

document.addEventListener("change", (event) => {
  if (event.target.matches("#economySelect")) {
    syncEconomyCustom();
  }
});

// --- Map drag-and-drop reorder (#12) ---
let draggedMapId = null;

document.addEventListener("dragstart", (event) => {
  const mapItem = event.target.closest(".map-item[data-id]");
  if (!mapItem) return;
  draggedMapId = mapItem.dataset.id;
  mapItem.classList.add("dragging");
  event.dataTransfer.effectAllowed = "move";
});

document.addEventListener("dragend", (event) => {
  draggedMapId = null;
  document.querySelectorAll(".map-item.dragging, .map-item.drag-over").forEach((el) => {
    el.classList.remove("dragging", "drag-over");
  });
});

document.addEventListener("dragover", (event) => {
  const mapItem = event.target.closest(".map-item[data-id]");
  if (!mapItem || mapItem.dataset.id === draggedMapId) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "move";
  document.querySelectorAll(".map-item.drag-over").forEach((el) => el.classList.remove("drag-over"));
  mapItem.classList.add("drag-over");
});

document.addEventListener("dragleave", (event) => {
  const mapItem = event.target.closest(".map-item[data-id]");
  if (mapItem) mapItem.classList.remove("drag-over");
});

document.addEventListener("drop", async (event) => {
  event.preventDefault();
  const targetItem = event.target.closest(".map-item[data-id]");
  if (!targetItem || !draggedMapId || targetItem.dataset.id === draggedMapId) return;
  document.querySelectorAll(".map-item.dragging, .map-item.drag-over").forEach((el) => {
    el.classList.remove("dragging", "drag-over");
  });
  const ids = state.maps.map((m) => m.id);
  const fromIdx = ids.indexOf(Number(draggedMapId));
  const toIdx = ids.indexOf(Number(targetItem.dataset.id));
  if (fromIdx < 0 || toIdx < 0) return;
  ids.splice(fromIdx, 1);
  ids.splice(toIdx, 0, Number(draggedMapId));
  draggedMapId = null;
  try {
    const data = await api("/api/maps/reorder", {
      method: "PUT",
      body: JSON.stringify({ order: ids }),
    });
    state.maps = normalizeMaps(data.maps || []);
    renderMapList();
    toast("\u5730\u56fe\u987a\u5e8f\u5df2\u66f4\u65b0");
  } catch (error) {
    toast(error.message, "error");
    renderMapList();
  }
});

document.addEventListener("keydown", async (event) => {
  // Ctrl+S / Cmd+S shortcut to save tactic
  if ((event.ctrlKey || event.metaKey) && event.key === "s") {
    event.preventDefault();
    const tab = currentTab();
    if (tab.type === "tactics" && getCurrentTactic(tab.side)) {
      const button = document.querySelector("[data-action='save-tactic']");
      if (button?.dataset.pending === "true") return;
      setButtonPending(button, true);
      try {
        await saveTactic(tab.side);
      } catch (error) {
        toast(error.message, "error");
      } finally {
        if (button?.isConnected) setButtonPending(button, false);
      }
    }
    return;
  }

  if ($("#mapModal").classList.contains("hidden")) return;
  if (event.key === "Escape") closeMapModal();
  if (event.key === "Enter" && event.target.matches("#mapNameInput")) {
    const button = document.querySelector("[data-action='save-map']");
    if (button?.dataset.pending === "true") return;
    setButtonPending(button, true);
    try {
      await saveMap();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      if (button?.isConnected) setButtonPending(button, false);
    }
  }
});

window.addEventListener("beforeunload", (event) => {
  autoSaveDraftToStorage();
  if (!hasUnsavedChanges()) return;
  event.preventDefault();
  event.returnValue = "";
});

// --- localStorage auto-save drafts ---

function draftStorageKey(mapId, side) {
  return `cs2t_draft_${mapId}_${side}`;
}

function autoSaveDraftToStorage() {
  const tab = currentTab();
  if (tab.type !== "tactics" || !state.selectedMapId) return;
  hydrateCurrentTacticFromForm();
  const tactic = getCurrentTactic(tab.side);
  if (!tactic) return;
  const snapshot = tacticSnapshotFromRenderedForm(tab.side, tactic);
  const isDraft = String(tactic.id).startsWith("draft-");
  if (isDraft && hasMeaningfulTacticDraft(snapshot)) {
    try {
      localStorage.setItem(draftStorageKey(state.selectedMapId, tab.side), JSON.stringify(snapshot));
    } catch { /* storage full */ }
  } else if (isDraft) {
    localStorage.removeItem(draftStorageKey(state.selectedMapId, tab.side));
  }
}

function restoreDraftFromStorage(mapId, side) {
  const key = draftStorageKey(mapId, side);
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const data = JSON.parse(raw);
    if (!data || !hasMeaningfulTacticDraft(data)) return null;
    const base = emptyTactic(side);
    const commands = Array.isArray(data.early_commands)
      ? data.early_commands
        .map((command) => {
          const priority = Number(command?.priority || 1);
          return {
            id: command?.id || uid("cmd"),
            priority: Number.isFinite(priority) ? Math.max(1, priority) : 1,
            text: String(command?.text || "").trim(),
          };
        })
        .filter((command) => command.text)
      : [];
    return {
      ...base,
      ...data,
      id: `draft-${side}`,
      map_id: mapId,
      side,
      tactic_tags: Array.isArray(data.tactic_tags) ? unique(data.tactic_tags) : base.tactic_tags,
      tactic_custom_tags: Array.isArray(data.tactic_custom_tags) ? unique(data.tactic_custom_tags) : [],
      early_commands: commands,
      decision_tree: normalizeDecisionTree(data.decision_tree || []),
    };
  } catch {
    return null;
  }
}

function clearDraftStorage(mapId, side) {
  localStorage.removeItem(draftStorageKey(mapId, side));
}

// Auto-save every 10 seconds
setInterval(autoSaveDraftToStorage, 10000);

// Clear draft storage after successful save
const _originalSaveTactic = saveTactic;
saveTactic = async function (side) {
  await _originalSaveTactic(side);
  if (state.selectedMapId) clearDraftStorage(state.selectedMapId, side);
};

checkAuth().catch((error) => showLogin(error.message));
