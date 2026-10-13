/* Markup helpers shared by every page: one chip, pill, button, row, bar and empty state each. */

const ICONS = {
  check: '<path d="m5 12 5 5 9-10"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  alert: '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5v.01"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  dash: '<path d="M7 12h10"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
  shield: '<path d="M12 3 5 6v5c0 4.5 3 8 7 10 4-2 7-5.5 7-10V6z"/><path d="m9 12 2 2 4-4"/>',
};

const icon = (name, size = 14) => `<svg class="ic" width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;
const spinner = () => '<span class="spin" aria-hidden="true"></span>';
const act = (name, data = {}) => `data-act="${name}"${Object.entries(data).map(([k, v]) => ` data-${k}="${esc(v)}"`).join("")}`;

const pill = (text, tone = "", lead = "") => `<span class="pill ${tone}">${lead}${esc(text)}</span>`;
const btn = (text, name, data, cls = "line sm") => `<button type="button" class="btn ${cls}" ${act(name, data)}>${esc(text)}</button>`;
const linkBtn = (text, href, cls = "line", label = "") => `<a class="btn ${cls}" href="${esc(href)}"${label ? ` aria-label="${esc(label)}"` : ""}>${esc(text)}</a>`;
/* An on/off switch; the label is its name for screen readers (it has no text of its own). */
const switchBtn = (on, label, attrs, disabled = false) =>
  `<button type="button" class="switch${on ? " on" : ""}" role="switch" aria-checked="${on}" aria-label="${esc(label)}"${disabled ? " disabled" : ""} ${attrs}></button>`;
const chip = (text, on, attrs, { count, sm } = {}) => {
  const n = count === undefined ? "" : ` <span class="n">${count}</span>`;
  return `<button type="button" class="chip${on ? " on" : ""}${sm ? " sm" : ""}" aria-pressed="${!!on}" ${attrs}>${on ? icon("check") : ""}${esc(text)}${n}</button>`;
};

const progress = (done, total, busy) => {
  const width = total ? Math.round((100 * done) / total) : 0;
  return `<div class="bar${busy ? " busy" : ""}" role="progressbar" aria-label="查找进度" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${done}"><i style="width:${width}%"></i></div>`;
};
const loading = (text) => `<div class="loading">${spinner()}<span>${esc(text)}</span></div>`;
const emptyState = (title, text, action = "") => `<div class="card empty"><h2>${esc(title)}</h2><p class="sub">${esc(text)}</p>${action}</div>`;
const skeletons = (n) => '<div class="card job skeleton" aria-hidden="true"><i></i><i></i><i></i></div>'.repeat(n);

/* A list row: text on the left (optionally a tick that makes the whole left side a toggle, or a `lead` before the text),
   anything on the right. */
function itemRow({ name, hint = "", end = "", pick = "", on = false, off = false, lead = "" }) {
  const text = `<span class="item-text"><b>${esc(name)}</b>${hint ? `<small>${esc(hint)}</small>` : ""}</span>`;
  const main = pick
    ? `<button type="button" class="item-main" aria-pressed="${on}" ${pick}><span class="tick">${on ? icon("check", 12) : ""}</span>${text}</button>`
    : `<div class="item-main">${lead}${text}</div>`;
  return `<div class="item${on ? " on" : ""}${off ? " off" : ""}">${main}${end ? `<div class="item-end">${end}</div>` : ""}</div>`;
}

/* A quiet toggle that shows or hides a block; its state lives in S.open so a re-render keeps it. */
function disclosure(id, title, body, hint = "") {
  const open = !!S.open[id];
  return `<button type="button" class="more" aria-expanded="${open}" aria-controls="${id}" ${act("toggle", { id })}>
    <span>${esc(title)}${hint ? `<small>${esc(hint)}</small>` : ""}</span>${icon("chevron", 18)}</button><div id="${id}" class="more-body"${open ? "" : " hidden"}>${body}</div>`;
}

const searchTitle = (s) => `${s.keyword} · ${s.city || "全国"}`;
const wholeJobs = (s) => s.whole;   // the jobs of a search that have every core element, each one once: the heading counts the same
const resultsHref = (s) => `#/results?${new URLSearchParams({ k: s.keyword, c: s.city || "" })}`;

/* What to say about one site of a search, or null when there is nothing to say. Tone "none" is a plain fact (the
   site found nothing, or only jobs that lack something); only a dead task or a login wall is a problem. A fact
   with `always` is shown even where the plain "found nothing" ones are not. */
function siteNote(x) {
  if (x.status === "waiting_login") return { tone: "warn", text: "需要先登录", end: btn("去登录", "login", { k: x.provider }) };
  if (x.status === "dead") {
    return { tone: "bad", text: x.reason, end: x.kind === "env" ? btn("如何配置", "how-keys") : btn("重试", "retry", { t: x.task_id }) };
  }
  // the reason's own "，稍后再试" would say "later" twice next to the promise of an automatic retry
  if (x.outcome === "retrying") return { tone: "warn", text: `${x.reason.replace(/，稍后再试$/, "")}，稍后会自动重试`, end: "" };
  if (x.status === "done" && !x.jobs) return { tone: "none", text: "这个网站没有找到", end: "" };
  if (x.status === "done" && !x.complete) {   // found something, but not one job is whole: say why instead of looking broken
    return { tone: "none", always: true, text: `找到 ${x.jobs} 个岗位，但信息都不全${x.why ? `（${x.why}）` : ""}`, end: "" };
  }
  return null;
}

const noteLine = (x, n) =>
  `<li class="note ${n.tone}">${icon(n.tone === "none" ? "dash" : "alert", 16)}<span><b>${esc(x.label)}</b> · ${esc(n.text)}</span>${n.end}</li>`;

/* the <ul> of per-site notes of a search; facts = also list the sites that simply found nothing */
function notesHtml(s, facts = false) {
  const lines = s.per_site.map((x) => [x, siteNote(x)]).filter(([, n]) => n && (facts || n.tone !== "none" || n.always)).map(([x, n]) => noteLine(x, n));
  return lines.length ? `<ul class="notes">${lines.join("")}</ul>` : "";
}

Object.assign(ACTIONS, {
  toggle: (el) => {
    const open = el.getAttribute("aria-expanded") !== "true";
    el.setAttribute("aria-expanded", open);
    $("#" + el.dataset.id).hidden = !open;
    S.open[el.dataset.id] = open;
  },
  retry: async (el) => {
    await api("POST /api/retry", { task_ids: [Number(el.dataset.t)] });
    toast("已重新开始查找", "ok");
    await route();
  },
  "how-keys": () => { location.hash = "#/settings?focus=keys"; },
});
