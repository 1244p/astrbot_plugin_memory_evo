const bridge = window.AstrBotPluginView;

const state = {
  scope: "",
  limit: 50,
  offset: 0,
  total: 0,
  items: [],
  filters: { q: "", kind: "", status: "" },
  care: null,
  persona: null,
  settings: null,
  settingsFilter: "",
  logs: { level: "", q: "", items: [], stats: null },
  snapshot: {},
};

const $ = (id) => document.getElementById(id);
const KIND_LABEL = {
  preference: "偏好",
  profile: "资料",
  fact: "事实",
  event: "事件",
  task: "任务",
};
const STATUS_LABEL = { active: "有效", suspect: "待确认", superseded: "已取代" };
const TRAIT_LABEL = {
  intimacy: "亲密度",
  playfulness: "玩心",
  directness: "直率",
  initiative: "主动",
  warmth: "温度",
};

function toast(msg, bad = false) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.toggle("bad", bad);
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.hidden = true), 2600);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function when(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())}`;
}

function clock(ts) {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/* ---------------- 载入 ---------------- */

async function loadScopes() {
  const data = await bridge.apiGet("scopes");
  const items = data.items || [];
  const sel = $("scope");
  sel.innerHTML = items
    .map((it) => `<option value="${esc(it.scope)}">${esc(it.label)}（${it.total} 条）</option>`)
    .join("");
  if (!items.length) {
    $("scope-hint").textContent = "还没有任何会话记忆，先去聊几句";
    $("facts-empty").hidden = false;
    return false;
  }
  state.scope = items[0].scope;
  sel.value = state.scope;
  return true;
}

async function loadOverview() {
  const d = await bridge.apiGet("overview", { scope: state.scope });
  state.care = d.care;
  state.persona = d.persona;
  const s = d.by_status || {};
  const active = s.active ?? 0;
  const suspect = s.suspect ?? 0;
  const live = Math.max(1, d.total - (d.superseded || 0));
  const vec = d.total ? Math.round((1 - d.no_embedding / live) * 100) : 0;
  const stats = [
    ["有效记忆", active, "accent"],
    ["待确认", suspect, suspect ? "warn" : ""],
    ["已取代", d.superseded ?? 0, ""],
    ["平均置信度", d.avg_confidence ?? 0, ""],
    ["向量覆盖", `${Math.max(0, Math.min(100, vec))}%`, ""],
    ["从未召回", d.never_hit ?? 0, ""],
  ];
  $("stats").innerHTML = stats
    .map(([label, value, cls]) => `
      <div class="stat ${cls}">
        <span class="stat-value">${esc(value)}</span>
        <span class="stat-label">${esc(label)}</span>
      </div>`)
    .join("");
  $("scope-hint").textContent = d.label || "";
  renderPersona();
  renderCare();
}

async function loadFacts() {
  const { q, kind, status } = state.filters;
  const data = await bridge.apiGet("facts", {
    scope: state.scope, q, kind, status, limit: state.limit, offset: state.offset,
  });
  state.items = data.items || [];
  state.total = data.total || 0;
  renderFacts();
}

/* ---------------- 记忆列表 ---------------- */

function renderFacts() {
  const list = $("facts-list");
  $("facts-empty").hidden = state.items.length > 0;
  $("list-count").textContent = state.total ? `记忆条目 · 共 ${state.total} 条` : "记忆条目";
  list.innerHTML = state.items
    .map((f) => {
      const cls = (f.status === "superseded" ? " is-superseded" : "") + (f.status === "suspect" ? " is-suspect" : "");
      const conf = Number(f.confidence ?? 0);
      const low = conf < 0.6 ? " low" : "";
      return `<li class="fact${cls}" data-id="${f.id}">
        <label class="check"><input type="checkbox" class="pick" value="${f.id}" /></label>
        <div class="fact-body">
          <div class="fact-text">
            <span class="slot">${esc(f.predicate)}</span><span class="obj">${esc(f.object)}</span>
          </div>
          <div class="fact-meta">
            <span>${esc(KIND_LABEL[f.kind] || f.kind)}</span>
            <span class="sep"></span>
            <span>重要 ${f.importance}</span>
            <span class="sep"></span>
            <span class="meter${low}" title="置信度"><i><b style="width:${Math.round(conf * 100)}%"></b></i>${conf}</span>
            <span class="sep"></span>
            <span>召回 ${f.hits} 次</span>
            <span class="sep"></span>
            <span>更新 ${esc(when(f.updated_at))}</span>
            <span class="sep"></span>
            <span class="mono">#${f.id}</span>
          </div>
        </div>
        <div class="fact-side">
          <span class="pill ${esc(f.status)}">${esc(STATUS_LABEL[f.status] || f.status)}</span>
          <div class="row-actions">
            ${f.status === "suspect" ? `<button class="btn quiet sm" data-act="confirm" data-id="${f.id}">确认</button>` : ""}
            <button class="btn quiet sm" data-act="delete" data-id="${f.id}">删</button>
          </div>
        </div>
      </li>`;
    })
    .join("");
  const from = state.total ? state.offset + 1 : 0;
  const to = Math.min(state.offset + state.limit, state.total);
  $("pager-info").textContent = `${from}–${to} / 共 ${state.total} 条`;
  $("prev").disabled = state.offset <= 0;
  $("next").disabled = state.offset + state.limit >= state.total;
  $("check-all").checked = false;
  syncSelection();
}

function selectedIds() {
  return [...document.querySelectorAll(".pick:checked")].map((el) => Number(el.value));
}

function syncSelection() {
  const ids = selectedIds();
  const bar = $("bulkbar");
  bar.hidden = ids.length === 0;
  $("bulk-count").textContent = `已选 ${ids.length} 条`;
  // 选择变了就撤销「再点一次确认」的待删状态，避免确认到别的条目
  if (pendingDelete.ids.length && !sameIds(pendingDelete.ids, ids)) {
    clearTimeout(pendingDelete.timer);
    pendingDelete = { ids: [], timer: 0 };
    paintDeleteArm([]);
  }
}

/* 删除要二次确认，但不能用 confirm()：插件视图在 sandbox="allow-scripts allow-forms
   allow-downloads" 的 iframe 里，没有 allow-modals，confirm() 会被浏览器直接忽略并返回
   false —— 于是点删除看起来「没反应」。改成页面内两段式：第一次点变成「再点一次确认」，
   6 秒内再点才真删。 */
let pendingDelete = { ids: [], timer: 0 };

function sameIds(a, b) {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

function paintDeleteArm(ids) {
  const armed = ids.length > 0;
  const bulk = $("delete-selected");
  if (bulk) bulk.textContent = armed ? `确认删除 ${ids.length} 条` : "删除";
  document.querySelectorAll('[data-act="delete"]').forEach((btn) => {
    const id = Number(btn.dataset.id);
    btn.textContent = armed && ids.includes(id) ? "确认" : "删";
  });
}

function armDelete(ids) {
  clearTimeout(pendingDelete.timer);
  pendingDelete = {
    ids,
    timer: setTimeout(() => {
      pendingDelete = { ids: [], timer: 0 };
      paintDeleteArm([]);
    }, 6000),
  };
  paintDeleteArm(ids);
  toast(`再点一次确认删除这 ${ids.length} 条`);
}

async function act(ids, kind) {
  if (!ids.length) return;
  if (kind === "delete" && !sameIds(pendingDelete.ids, ids)) {
    armDelete(ids);
    return;
  }
  clearTimeout(pendingDelete.timer);
  pendingDelete = { ids: [], timer: 0 };
  paintDeleteArm([]);
  try {
    const r = await bridge.apiPost(`facts/${kind}`, { scope: state.scope, ids });
    if (kind === "delete") toast(`已删除 ${r.deleted} 条`);
    else toast(`已确认 ${r.confirmed} 条`);
    await reload();
  } catch (e) {
    toast(e.message, true);
  }
}

/* ---------------- 人格 ---------------- */

function renderPersona() {
  const p = state.persona;
  if (!p) return;
  const traits = p.traits || {};
  const base = p.baseline || {};
  $("persona-traits").innerHTML = Object.keys(TRAIT_LABEL)
    .map((k) => {
      const v = Math.round((traits[k] ?? 0.5) * 100);
      const b = Math.round((base[k] ?? 0.5) * 100);
      const d = v - b;
      return `<div class="trait">
        <div class="trait-head">
          <span class="trait-name">${TRAIT_LABEL[k]}</span>
          <span class="trait-num">${v}</span>
        </div>
        <div class="trait-bar"><i style="width:${v}%"></i><b style="left:${b}%"></b></div>
        <div class="trait-foot">基线 ${b} · 偏离 ${d > 0 ? "+" : ""}${d}</div>
      </div>`;
    })
    .join("");

  const log = (p.log || []).slice();
  if (!log.length) {
    $("persona-log").innerHTML = `<li><div class="tl-reason hint">还没有演化记录，人格仍是初始值。</div></li>`;
    return;
  }
  $("persona-log").innerHTML = log
    .map((row, i) => {
      const cur = row.traits || {};
      const prevRaw = i > 0 ? log[i - 1].traits || {} : null;
      const chips = Object.keys(TRAIT_LABEL)
        .map((k) => {
          const now = Math.round((cur[k] ?? 0.5) * 100);
          const before = prevRaw ? Math.round((prevRaw[k] ?? 0.5) * 100) : Math.round((base[k] ?? 0.5) * 100);
          const d = now - before;
          if (!d) return "";
          return `<span class="delta${d < 0 ? " down" : ""}">${TRAIT_LABEL[k]} ${d > 0 ? "+" : ""}${d}</span>`;
        })
        .filter(Boolean)
        .join("");
      return `<li>
        <div class="tl-head">
          <span class="tl-ver">v${row.version}</span>
          <span class="tl-reason">${esc(row.reason || "（无说明）")}</span>
          <span class="tl-time">${esc(when(row.created_at))}</span>
        </div>
        ${chips ? `<div class="tl-deltas">${chips}</div>` : ""}
      </li>`;
    })
    .join("");
}

/* ---------------- 主动消息 ---------------- */

const REASON_TEXT = {
  never_sent: "还没主动发过，只看空闲",
  gap: "上次主动设定的间隔还没到",
  idle: "等你安静够久",
};

function nextText(c) {
  if (c.ready || !c.next_at) return "现在就可以";
  const at = c.next_at;
  const mins = Math.max(1, Math.ceil((at - Date.now() / 1000) / 60));
  const today = new Date(at * 1000).toDateString() === new Date().toDateString();
  return `${today ? "" : "明天 "}${clock(at)}（${mins} 分钟后）`;
}

function renderCare() {
  const c = state.care || {};
  const s = c.settings || {};
  const hero = $("care-hero");
  let cls = "wait";
  let title = "";
  if (!s.enabled) {
    title = "总开关关着（care_enable）";
  } else if (!c.enabled) {
    title = "这个会话的主动消息关着";
  } else if (c.ready) {
    cls = "ready";
    title = "现在就可以主动开口";
  } else {
    title = `还要等 ${Math.max(1, Math.ceil(((c.next_at || 0) - Date.now() / 1000) / 60))} 分钟`;
  }
  hero.className = `care-hero ${cls}`;
  $("care-mark").textContent = cls === "ready" ? "✓" : "…";
  $("care-state").textContent = title;
  const basis = REASON_TEXT[c.next_reason] || "空闲与间隔都满足";
  const checked = c.checked_at
    ? `上次检查 ${when(c.checked_at)} → ${c.check_text || c.check_reason || "—"}`
    : "后台还没检查过（心跳没起来？）";
  $("care-because").textContent = `${basis} · 下次最早 ${nextText(c)} · ${checked}`;

  // 后端没给的项直接不渲染，避免出现一排「— 字」这种半截话
  const gates = [];
  const gate = (k, v) => {
    if (v === undefined || v === null || v === "") return;
    gates.push([k, v]);
  };
  gate("今日已发", `${c.sent_today ?? 0} / ${s.daily_max ?? "—"}`);
  gate("连续未回应", `${c.unanswered ?? 0} / ${s.max_unanswered ?? "—"}`);
  gate("空闲要求", `${s.min_idle_minutes ?? c.min_idle_minutes ?? "—"} 分钟`);
  gate("免打扰", s.quiet_start ? `${s.quiet_start} – ${s.quiet_end}` : "");
  gate("允许时段", s.schedule || "不限");
  gate("发送概率", s.probability);
  gate("单条上限", s.max_chars ? `${s.max_chars} 字` : "");
  gate("判定依据", REASON_TEXT[c.next_reason] || "");
  $("care-grid").innerHTML = gates
    .map(([k, v]) => `<div class="gate"><span class="gate-k">${esc(k)}</span><span class="gate-v">${esc(v)}</span></div>`)
    .join("");

  const last = $("care-last");
  if (c.last_text) {
    last.hidden = false;
    last.innerHTML = `<b>上次主动说</b>${esc(c.last_text)}`;
  } else {
    last.hidden = true;
  }

  $("care-toggle-input").checked = Boolean(c.enabled);
  $("care-toggle-label").textContent = c.enabled ? "这个会话已开启" : "这个会话已关闭";
}

/* ---------------- 设置 ---------------- */

function fieldInput(f) {
  const id = `set-${f.key}`;
  const common = `id="${id}" data-key="${esc(f.key)}"`;
  if (f.type === "bool") {
    return `<input type="checkbox" ${common} ${f.value ? "checked" : ""} />`;
  }
  if (f.type === "int" || f.type === "float") {
    const step = f.type === "int" ? "1" : "0.01";
    const min = f.min !== undefined ? `min="${f.min}"` : "";
    const max = f.max !== undefined ? `max="${f.max}"` : "";
    return `<input type="number" ${common} step="${step}" ${min} ${max} value="${esc(f.value ?? "")}" />`;
  }
  return `<input type="text" ${common} value="${esc(f.value ?? "")}" />`;
}

function renderSettings() {
  const data = state.settings;
  if (!data) {
    $("settings-groups").innerHTML = `<p class="empty">设置加载失败，刷新试试。</p>`;
    return;
  }
  const needle = state.settingsFilter.trim().toLowerCase();
  const match = (f) =>
    !needle ||
    [f.key, f.label, f.hint].filter(Boolean).some((s) => String(s).toLowerCase().includes(needle));

  const groups = (data.groups || [])
    .map((g) => ({ ...g, fields: (g.fields || []).filter(match) }))
    .filter((g) => g.fields.length);

  if (!groups.length) {
    $("settings-groups").innerHTML = `<p class="empty">没有匹配「${esc(needle)}」的配置项。</p>`;
    return;
  }

  $("settings-groups").innerHTML = groups
    .map((g) => `
      <section class="set-group">
        <h2 class="sec">${esc(g.title)}<span class="hint">${g.fields.length} 项</span></h2>
        <div class="set-fields">
          ${g.fields.map((f) => `
            <div class="set-field${f.type === "bool" ? " is-bool" : ""}" data-field="${esc(f.key)}">
              <div class="field-head">
                <label for="set-${esc(f.key)}">
                  ${esc(f.label)}
                  ${f.restart ? `<span class="badge">需重启</span>` : ""}
                </label>
                ${f.type === "bool" ? fieldInput(f) : ""}
              </div>
              ${f.type === "bool" ? "" : fieldInput(f)}
              ${f.hint ? `<p class="hint">${esc(f.hint)}</p>` : ""}
              <p class="field-error" data-error="${esc(f.key)}" hidden></p>
            </div>`).join("")}
        </div>
      </section>`)
    .join("");
  state.snapshot = readSettingsForm();
  updateDirty();
}

function readSettingsForm() {
  const values = {};
  document.querySelectorAll("#settings-groups [data-key]").forEach((el) => {
    values[el.dataset.key] = el.type === "checkbox" ? el.checked : el.value;
  });
  return values;
}

function updateDirty() {
  const now = readSettingsForm();
  const changed = Object.keys(now).filter((k) => {
    const before = state.snapshot[k];
    if (before === undefined) return false;
    if (typeof now[k] === "boolean" || typeof before === "boolean") return Boolean(now[k]) !== Boolean(before);
    return String(now[k]) !== String(before);
  });
  const el = $("settings-dirty");
  el.classList.toggle("on", changed.length > 0);
  el.textContent = changed.length ? `已改 ${changed.length} 项，待保存` : "没有改动";
  return changed;
}

function clearFieldErrors() {
  document.querySelectorAll("[data-error]").forEach((el) => {
    el.hidden = true;
    el.textContent = "";
  });
  document.querySelectorAll(".set-field.has-error").forEach((el) => el.classList.remove("has-error"));
}

async function saveSettings() {
  clearFieldErrors();
  $("settings-save").disabled = true;
  try {
    const r = await bridge.apiPost("settings", { values: readSettingsForm() });
    if (r && r.ok === false) {
      const errors = r.errors || {};
      Object.entries(errors).forEach(([key, msg]) => {
        const holder = document.querySelector(`.set-field[data-field="${key}"]`);
        const el = document.querySelector(`[data-error="${key}"]`);
        if (holder) holder.classList.add("has-error");
        if (el) {
          el.textContent = msg;
          el.hidden = false;
        }
      });
      toast(`有 ${Object.keys(errors).length} 项没通过校验`, true);
      return;
    }
    state.settings = r;
    renderSettings();
    const changed = r.changed || [];
    if (!changed.length) {
      toast("没有改动");
    } else if ((r.restart_required || []).length) {
      toast(`已保存；${r.restart_required.join("、")} 需重启生效`);
    } else {
      toast(`已保存：${changed.length} 项`);
    }
  } catch (e) {
    toast(e.message || "保存失败", true);
  } finally {
    $("settings-save").disabled = false;
  }
}

async function loadSettings(force = false) {
  if (state.settings && !force) {
    renderSettings();
    return;
  }
  try {
    state.settings = await bridge.apiGet("settings");
    renderSettings();
  } catch (e) {
    toast(e.message || "设置加载失败", true);
  }
}

/* ---------------- 日志 ---------------- */

const LOG_LEVELS = ["", "DEBUG", "INFO", "WARN", "ERROR"];

function renderLogLevels() {
  $("logs-levels").innerHTML = LOG_LEVELS.map((lv) => {
    const on = state.logs.level === lv ? " on" : "";
    return `<button class="chip${on}" data-level="${lv}">${lv || "全部"}</button>`;
  }).join("");
}

async function loadLogs() {
  try {
    const data = await bridge.apiGet("logs", {
      limit: 200,
      level: state.logs.level,
      q: state.logs.q,
    });
    state.logs.items = data.items || [];
    state.logs.stats = data.stats || null;
    renderLogs();
  } catch (e) {
    toast(e.message || "日志加载失败", true);
  }
}

function fmtValue(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function renderLogs() {
  const { items, stats } = state.logs;
  $("logs-list").innerHTML = items.map((r) => {
    const f = r.f || {};
    const fields = Object.keys(f)
      .map((k) => `<span class="log-field">${esc(k)}=${esc(fmtValue(f[k]))}</span>`)
      .join("");
    return `<li class="log ${esc(String(r.lvl || "INFO").toLowerCase())}">
      <span class="log-time">${esc(r.time || "")}</span>
      <span class="log-level">${esc(r.lvl || "INFO")}</span>
      <span class="log-evt">${esc(r.evt || "")}</span>
      <span class="log-msg">${esc(r.msg || "")}</span>
      ${fields ? `<span class="log-fields">${fields}</span>` : ""}
    </li>`;
  }).join("");
  $("logs-empty").hidden = items.length > 0;
  if (!stats) return;
  const c = stats.counts || {};
  const top = Object.entries(stats.events || {})
    .slice(0, 5)
    .map(([k, v]) => `${k}×${v}`)
    .join("、");
  $("logs-stats").textContent =
    `${stats.day}：显示 ${items.length} 条 · ` +
    `DEBUG ${c.DEBUG || 0} / INFO ${c.INFO || 0} / WARN ${c.WARN || 0} / ERROR ${c.ERROR || 0}` +
    ` · 记录级别 ${stats.level}，保留 ${stats.days} 天` +
    (top ? ` · 今天高频：${top}` : "");
}

/* ---------------- 主流程 ---------------- */

async function reload() {
  if (!state.scope) return;
  try {
    await Promise.all([loadOverview(), loadFacts()]);
  } catch (e) {
    toast(e.message || "加载失败", true);
  }
}

function bind() {
  $("scope").addEventListener("change", async (e) => {
    state.scope = e.target.value;
    state.offset = 0;
    await reload();
  });
  $("refresh").addEventListener("click", reload);

  // 搜索：输入即搜（防抖），不用按钮
  let timer = null;
  $("q").addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      state.filters = { ...state.filters, q: $("q").value.trim() };
      state.offset = 0;
      loadFacts().catch((e) => toast(e.message, true));
    }, 260);
  });

  $("kind-chips").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    [...$("kind-chips").children].forEach((c) => c.classList.toggle("on", c === chip));
    state.filters = { ...state.filters, kind: chip.dataset.kind };
    state.offset = 0;
    loadFacts().catch((err) => toast(err.message, true));
  });

  $("status-chips").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    [...$("status-chips").children].forEach((c) => c.classList.toggle("on", c === chip));
    state.filters = { ...state.filters, status: chip.dataset.status };
    state.offset = 0;
    loadFacts().catch((err) => toast(err.message, true));
  });

  $("prev").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - state.limit);
    loadFacts().catch((e) => toast(e.message, true));
  });
  $("next").addEventListener("click", () => {
    state.offset += state.limit;
    loadFacts().catch((e) => toast(e.message, true));
  });

  $("facts-list").addEventListener("change", (e) => {
    if (e.target.classList.contains("pick")) syncSelection();
  });
  $("facts-list").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    act([Number(btn.dataset.id)], btn.dataset.act);
  });
  $("check-all").addEventListener("change", (e) => {
    document.querySelectorAll(".pick").forEach((el) => (el.checked = e.target.checked));
    syncSelection();
  });
  $("bulk-clear").addEventListener("click", () => {
    document.querySelectorAll(".pick").forEach((el) => (el.checked = false));
    $("check-all").checked = false;
    syncSelection();
  });
  $("delete-selected").addEventListener("click", () => act(selectedIds(), "delete"));
  $("confirm-selected").addEventListener("click", () => act(selectedIds(), "confirm"));

  $("export").addEventListener("click", () => {
    bridge
      .download("export", { scope: state.scope }, `memory-${state.scope.replace(/:/g, "_")}.json`)
      .catch((e) => toast(e.message, true));
  });

  $("settings-save").addEventListener("click", saveSettings);
  $("settings-groups").addEventListener("input", updateDirty);
  $("settings-groups").addEventListener("change", updateDirty);
  $("settings-filter").addEventListener("input", () => {
    state.settingsFilter = $("settings-filter").value;
    renderSettings();
  });
  $("settings-defaults").addEventListener("click", () => {
    const groups = (state.settings && state.settings.groups) || [];
    groups.forEach((g) => (g.fields || []).forEach((f) => {
      const el = document.getElementById(`set-${f.key}`);
      if (!el) return;
      if (el.type === "checkbox") el.checked = Boolean(f.default);
      else el.value = f.default ?? "";
    }));
    updateDirty();
    toast("已填入默认值，记得保存");
  });

  $("care-toggle-input").addEventListener("change", async () => {
    try {
      const r = await bridge.apiPost("care", {
        scope: state.scope,
        enabled: $("care-toggle-input").checked,
      });
      state.care = { ...state.care, ...r };
      renderCare();
      toast(r.enabled ? "已开启" : "已关闭");
    } catch (e) {
      toast(e.message, true);
      $("care-toggle-input").checked = Boolean(state.care && state.care.enabled);
    }
  });

  $("logs-levels").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    state.logs.level = chip.dataset.level;
    renderLogLevels();
    loadLogs();
  });
  let logTimer = null;
  $("logs-filter").addEventListener("input", () => {
    clearTimeout(logTimer);
    logTimer = setTimeout(() => {
      state.logs.q = $("logs-filter").value.trim();
      loadLogs();
    }, 260);
  });
  $("logs-reload").addEventListener("click", loadLogs);
  $("logs-clear").addEventListener("click", async () => {
    try {
      const r = await bridge.apiPost("logs/clear", {});
      toast(`已清空 ${r.removed || 0} 个日志文件`);
      await loadLogs();
    } catch (e) {
      toast(e.message, true);
    }
  });

  $("tabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".tab");
    if (!tab) return;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    const name = tab.dataset.tab;
    for (const key of ["facts", "persona", "care", "logs", "settings"]) {
      $(`panel-${key}`).hidden = key !== name;
    }
    if (name === "settings") loadSettings();
    if (name === "logs") {
      renderLogLevels();
      loadLogs();
    }
  });
}

(async function main() {
  try {
    const ctx = await bridge.ready();
    document.documentElement.dataset.theme = ctx.isDark ? "dark" : "light";
    document.title = ctx.pageTitle || "记忆演化";
  } catch (e) {
    /* 非 iframe 打开时没有 bridge，用默认主题继续 */
  }
  bind();
  try {
    if (await loadScopes()) await reload();
  } catch (e) {
    toast(e.message || "初始化失败", true);
  }
})();
