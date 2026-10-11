/* 查找: the search box, the site picker and the recent searches with their per-site progress. */

const EXAMPLES = ["前端工程师", "Python 开发", "数据分析", "产品经理", "运营"];
const GROUPS = [["login", "登录后看得更全"], ["city", "需要填城市"], ["keys", "需要先配置密钥"]];
const SITE_ICON = { done: ["check", "ok"], running: ["spin", "run"], pending: ["clock", ""], waiting_login: ["alert", "warn"], dead: ["alert", "bad"] };

/* ---- site picker ---- */
const usable = (p) => p.configured !== false && (p.auth !== "login" || p.logged_in);

function siteGroups() {
  const g = { common: [], login: [], city: [], keys: [] };
  for (const p of S.providers) {
    g[p.configured === false ? "keys" : p.auth !== "public" ? "login" : p.requires_city ? "city" : "common"].push(p);
  }
  return g;
}

const pickRow = (p, hint, end = "") => itemRow({ name: p.label, hint, end, pick: act("pick", { k: p.name }), on: S.picked.has(p.name) });

function loginRow(p) {
  const hint = p.logged_in ? "" : `会弹出一个浏览器窗口；${LOGIN_HINT[p.auth]}`;
  const end = p.logged_in ? pill("已登录", "ok") : btn("去登录", "login", { k: p.name });
  return usable(p) ? pickRow(p, hint, end) : itemRow({ name: p.label, hint, end, off: true });   // login required first: cannot be ticked yet
}

const SITE_ROW = {
  login: loginRow,
  city: (p) => pickRow(p, "要先在上面填好城市"),
  keys: (p) => itemRow({ name: p.label, hint: "还没配置密钥", end: btn("如何配置", "how-keys"), off: true }),
};

function moreSites(g) {
  return GROUPS.filter(([k]) => g[k].length)
    .map(([k, title]) => `<div class="group"><h3>${title}</h3><div class="items">${g[k].map(SITE_ROW[k]).join("")}</div></div>`).join("");
}

function sitePicker() {
  const g = siteGroups();
  const more = moreSites(g);
  const common = g.common.map((p) => chip(p.label, S.picked.has(p.name), act("pick", { k: p.name }))).join("");
  return `<h2 class="label">常用网站<small>已经选好了，点一下可以取消</small></h2><div class="chips">${common}</div>${more ? disclosure("more", "更多网站", more) : ""}`;
}

/* ---- recent searches ---- */
function searchState(s) {
  if (s.status === "running") return ["查找中", "", true];
  if (s.status === "waiting_login") return ["等你登录", "warn"];
  return s.per_site.every((x) => x.status === "dead") ? ["没成功", "bad"] : ["已完成", "ok"];
}

function siteDot(x) {
  const [ico, tone] = x.status === "done" && !x.jobs ? ["dash", ""] : SITE_ICON[x.status];
  const tail = x.jobs ? `<b>${x.jobs}</b>` : x.status === "done" ? '<span class="none">没有找到</span>' : "";
  return `<li class="dot ${tone}">${ico === "spin" ? spinner() : icon(ico)}<span>${esc(x.label)}</span>${tail}</li>`;
}

function searchCard(s) {
  const [text, tone, busy] = searchState(s);
  const done = s.per_site.filter((x) => x.status === "done" || x.status === "dead").length;
  return `<article class="card search-card">
    <div class="row"><h3>${esc(searchTitle(s))}</h3>${pill(text, tone, busy ? spinner() : "")}</div>
    ${progress(done, s.per_site.length, busy)}
    <p class="sub small">已完成 ${done} / ${s.per_site.length} 个网站 · 已找到 ${s.jobs_total} 个岗位</p>
    <ul class="dots">${s.per_site.map(siteDot).join("")}</ul>${notesHtml(s)}
    <div class="actions">${linkBtn("查看结果", resultsHref(s), "line", `查看结果：${searchTitle(s)}`)}</div></article>`;
}

const FIRST_RUN = '<p class="sub first-run">还没有查找记录。填好上面的岗位，点“开始查找”就行。</p>';

async function refreshSearches(e) {
  const { searches } = await api("GET /api/searches", { limit: 6 });
  if (e !== epoch) return false;
  paint($("#recent"), searches.length ? searches.map(searchCard).join("") : FIRST_RUN);
  return searches.some((s) => s.status === "running");
}

/* ---- the page ---- */
const searchPage = () => `<section class="narrow search">
  <div class="hero"><h1>想找什么工作？</h1><p class="sub">告诉我岗位和城市，我帮你到几个招聘网站一起找。</p></div>
  <form class="card searchbar" data-submit="search" novalidate>
    <label class="field"><span>岗位或关键词</span><input id="kw" name="keyword" placeholder="例如：前端工程师" autocomplete="off"></label>
    <label class="field"><span>城市</span><input id="city" name="city" placeholder="例如：杭州（不填=全国）" autocomplete="off"></label>
    <button type="submit" class="btn primary">开始查找</button></form>
  <div class="examples"><span class="sub">试试：</span>${EXAMPLES.map((v) => chip(v, false, act("example", { v }), { sm: true })).join("")}</div>
  <div class="msg" id="msg" role="alert"></div>
  <div id="sites"></div>
  <h2 class="label">最近查找</h2><div class="grid2" id="recent"></div></section>`;

async function viewSearch(params, e) {
  S.picked ||= new Set(siteGroups().common.map((p) => p.name));
  $("#view").innerHTML = searchPage();
  $("#kw").value = S.form.keyword;
  $("#city").value = S.form.city;
  paint($("#sites"), sitePicker());
  $("#kw").focus();
  if (await refreshSearches(e)) pollWhile(refreshSearches);
}

function searchProblem(keyword, city, sites) {
  if (!keyword) return "请先填写岗位关键词";
  if (!sites.length) return "请至少选择一个网站";
  const needCity = sites.filter((p) => p.requires_city);
  return !city && needCity.length ? `${needCity.map((p) => p.label).join("、")}需要先填写城市` : "";
}

async function startSearch(form) {
  const keyword = $("#kw").value.trim();
  const city = $("#city").value.trim();
  const sites = S.providers.filter((p) => S.picked.has(p.name) && usable(p));
  const problem = searchProblem(keyword, city, sites);
  $("#msg").textContent = problem;
  if (problem) {
    if (!keyword) $("#kw").focus();
    return;
  }
  const go = form.querySelector("[type=submit]");
  go.disabled = true;
  try {
    await api("POST /api/crawl", { keyword, city, providers: sites.map((p) => p.name) });
    if (await refreshSearches(epoch)) pollWhile(refreshSearches);
    $("#recent").firstElementChild?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  } finally {
    go.disabled = false;
  }
}

Object.assign(ACTIONS, {
  search: startSearch,
  pick: (el) => {
    const k = el.dataset.k;
    S.picked.has(k) ? S.picked.delete(k) : S.picked.add(k);
    paint($("#sites"), sitePicker());
    $(`#sites [data-act="pick"][data-k="${k}"]`)?.focus();   // the repaint replaced the button the keyboard was on
  },
  example: (el) => {
    S.form.keyword = $("#kw").value = el.dataset.v;
    $("#kw").focus();
  },
});
