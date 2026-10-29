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

function when(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const diff = (Date.now() - d.getTime()) / 1000;
  const abs = d.toLocaleString();
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return abs.slice(0, 16);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

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
  const vec = d.total ? Math.round((1 - d.no_embedding / Math.max(1, d.total - (s.superseded || 0))) * 100) : 0;
  const cards = [
    ["有效记忆", s.active ?? 0, "primary"],
    ["待确认", s.suspect ?? 0, s.suspect ? "warn" : ""],
    ["已取代", d.superseded ?? 0, ""],
    ["平均置信度", d.avg_confidence ?? 0, ""],
    ["向量覆盖", `${Math.max(0, Math.min(100, vec))}%`, ""],
    ["从未召回", d.never_hit ?? 0, ""],
  ];
  $("stats").innerHTML = cards
    .map(([label, value, cls]) => `
      <div class="card ${cls}">
        <span class="card-value">${esc(value)}</span>
        <span class="card-label">${esc(label)}</span>
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

function renderFacts() {
  const body = $("facts-body");
  $("facts-empty").hidden = state.items.length > 0;
  body.innerHTML = state.items
    .map((f) => {
      const dim = f.status === "superseded" ? " dim" : "";
      return `<tr class="${dim.trim()}" data-id="${f.id}">
        <td class="c"><input type="checkbox" class="pick" value="${f.id}" /></td>
        <td class="c mono">${f.id}</td>
        <td><span class="slot">${esc(f.predicate)}</span> ${esc(f.object)}</td>
        <td class="c"><span class="tag">${esc(KIND_LABEL[f.kind] || f.kind)}</span></td>
        <td class="c mono">${f.importance}</td>
        <td class="c mono">${f.confidence}</td>
        <td class="c mono">${f.hits}</td>
        <td class="c"><span class="status ${esc(f.status)}">${esc(STATUS_LABEL[f.status] || f.status)}</span></td>
        <td class="c nowrap">${esc(when(f.updated_at))}</td>
      </tr>`;
    })
    .join("");
  const from = state.total ? state.offset + 1 : 0;
  const to = Math.min(state.offset + state.limit, state.total);
  $("pager-info").textContent = `${from}–${to} / 共 ${state.total} 条`;
  $("prev").disabled = state.offset <= 0;
  $("next").disabled = state.offset + state.limit >= state.total;
}

function renderPersona() {
  const p = state.persona;
  if (!p) return;
  const traits = p.traits || {};
  const base = p.baseline || {};
  $("persona-traits").innerHTML = Object.keys(TRAIT_LABEL)
    .map((k) => {
      const v = Math.round((traits[k] ?? 0.5) * 100);
      const b = Math.round((base[k] ?? 0.5) * 100);
      return `<div class="trait">
        <div class="trait-head"><span>${TRAIT_LABEL[k]}</span><span class="mono">${v}</span></div>
        <div class="trait-bar"><i style="width:${v}%"></i><b style="left:${b}%"></b></div>
      </div>`;
    })
    .join("");
  $("persona-log").innerHTML = (p.log || [])
    .slice()
    .reverse()
    .map((row) => {
      const t = row.traits || {};
      return `<li>
        <span class="mono v">v${row.version}</span>
        <span class="reason">${esc(row.reason || "（无说明）")}</span>
        <span class="hint">${esc(when(row.created_at))}</span>
        <span class="traits-mini">${Object.keys(TRAIT_LABEL).map((k) => `${TRAIT_LABEL[k]} ${Math.round((t[k] ?? 0.5) * 100)}`).join(" · ")}</span>
      </li>`;
    })
    .join("") || `<li class="hint">还没有演化记录，人格仍是初始值。</li>`;
}

function clock(ts) {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

const REASON_TEXT = {
  never_sent: "还没主动发过，只看空闲",
  gap: "上次主动设定的间隔还没到",
  idle: "等你安静够久",
};

function nextText(c) {
  const at = c.next_at || 0;
  if (!at) return "现在就可以";
  if (c.ready) return "现在就可以";
  const mins = Math.max(1, Math.ceil((at - Date.now() / 1000) / 60));
  const label = clock(at);
  const today = new Date(at * 1000).toDateString() === new Date().toDateString();
  return `${today ? "" : "明天 "}${label}（${mins} 分钟后）`;
}

function renderCare() {
  const c = state.care || {};
  const s = (c.settings || {});
  const rows = [
    ["本会话", c.enabled ? "开" : "关"],
    ["今日已发", `${c.sent_today ?? 0} / ${s.daily_max ?? "—"}`],
    ["连续未回应", `${c.unanswered ?? 0} / ${s.max_unanswered ?? "—"}`],
    ["下次最早", nextText(c)],
    ["判定依据", REASON_TEXT[c.next_reason] || "空闲 + 间隔都满足"],
    ["空闲要求", `${s.min_idle_minutes ?? c.min_idle_minutes ?? "—"} 分钟`],
    ["免打扰", s.quiet_start ? `${s.quiet_start} - ${s.quiet_end}` : "—"],
    ["允许时段", s.schedule || "不限"],
    ["总开关", s.enabled ? "开" : "关"],
  ];
  $("care-grid").innerHTML = rows
    .map(([k, v]) => `<div class="care-cell"><span class="care-k">${esc(k)}</span><span class="care-v">${esc(v)}</span></div>`)
    .join("");
  const last = c.last_text ? `<div class="care-last">上次主动说：${esc(c.last_text)}</div>` : "";
  $("care-grid").insertAdjacentHTML("beforeend", last);
  $("care-grid").insertAdjacentHTML(
    "beforeend",
    `<div class="care-last">下次最早 = max(距上次主动的间隔, 距用户最后一句的空闲要求)；
     免打扰与允许时段另算——即使时间到了，也不一定就在窗口里。</div>`,
  );
  $("care-toggle").textContent = c.enabled ? "关闭这个会话的主动消息" : "开启这个会话的主动消息";
}

async function reload() {
  if (!state.scope) return;
  try {
    await Promise.all([loadOverview(), loadFacts()]);
  } catch (e) {
    toast(e.message || "加载失败", true);
  }
}

function selectedIds() {
  return [...document.querySelectorAll(".pick:checked")].map((el) => Number(el.value));
}

function bind() {
  $("scope").addEventListener("change", async (e) => {
    state.scope = e.target.value;
    state.offset = 0;
    await reload();
  });
  $("refresh").addEventListener("click", reload);
  $("search").addEventListener("click", () => {
    state.filters = { q: $("q").value.trim(), kind: $("kind").value, status: $("status").value };
    state.offset = 0;
    loadFacts().catch((e) => toast(e.message, true));
  });
  $("q").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("search").click();
  });
  $("prev").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - state.limit);
    loadFacts().catch((e) => toast(e.message, true));
  });
  $("next").addEventListener("click", () => {
    state.offset += state.limit;
    loadFacts().catch((e) => toast(e.message, true));
  });
  $("check-all").addEventListener("change", (e) => {
    document.querySelectorAll(".pick").forEach((el) => (el.checked = e.target.checked));
  });
  $("delete-selected").addEventListener("click", async () => {
    const ids = selectedIds();
    if (!ids.length) return toast("先选中要删的条目", true);
    if (!confirm(`删除这 ${ids.length} 条记忆？删了就找不回来。`)) return;
    try {
      const r = await bridge.apiPost("facts/delete", { scope: state.scope, ids });
      toast(`已删除 ${r.deleted} 条`);
      await reload();
    } catch (e) {
      toast(e.message, true);
    }
  });
  $("confirm-selected").addEventListener("click", async () => {
    const ids = selectedIds();
    if (!ids.length) return toast("先选中待确认的条目", true);
    try {
      const r = await bridge.apiPost("facts/confirm", { scope: state.scope, ids });
      toast(`已确认 ${r.confirmed} 条`);
      await reload();
    } catch (e) {
      toast(e.message, true);
    }
  });
  $("export").addEventListener("click", () => {
    bridge
      .download("export", { scope: state.scope }, `memory-${state.scope.replace(/:/g, "_")}.json`)
      .catch((e) => toast(e.message, true));
  });
  $("care-toggle").addEventListener("click", async () => {
    try {
      const r = await bridge.apiPost("care", {
        scope: state.scope,
        enabled: !(state.care && state.care.enabled),
      });
      state.care = { ...state.care, ...r };
      renderCare();
      toast(r.enabled ? "已开启" : "已关闭");
    } catch (e) {
      toast(e.message, true);
    }
  });
  $("tabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".tab");
    if (!tab) return;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    const name = tab.dataset.tab;
    for (const key of ["facts", "persona", "care"]) {
      $(`panel-${key}`).hidden = key !== name;
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
