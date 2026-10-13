/* 查找: the search box, the newest search with its per-site progress right under it, the site picker (one quiet line
   once a search exists) and the earlier searches. */

const EXAMPLES = ["前端工程师", "Python 开发", "数据分析", "产品经理", "运营"];
/* what the sites of "更多网站" ask of the user first, in the order they are listed */
const GROUPS = [["login", "要先登录"], ["window", "会弹出浏览器窗口"], ["city", "需要填城市"], ["keys", "需要先配置密钥"]];
const SITE_ICON = { done: ["check", "ok"], running: ["spin", "run"], pending: ["clock", ""], waiting_login: ["alert", "warn"], dead: ["alert", "bad"] };

/* ---- site picker ---- */
const usable = (p) => p.configured !== false && (p.auth !== "login" || p.logged_in);

/* Where a site is listed follows what it asks of the user. The common sites ask nothing: they work as they are (a login
   only adds to what an optional one finds). Every other site says what it needs first. The first search ticks the
   common sites and the ones that only need a city: those are searched whenever a city is typed. */
function groupOf(p) {
  if (p.configured === false) return "keys";
  if (p.auth === "login") return "login";
  if (p.needs_headed) return "window";
  return p.requires_city ? "city" : "common";
}

function siteGroups() {
  const g = { common: [], login: [], window: [], city: [], keys: [] };
  for (const p of S.providers) g[groupOf(p)].push(p);
  return g;
}

const pickRow = (p, hint, end = "") => itemRow({ name: p.label, hint, end, pick: act("pick", { k: p.name }), on: S.picked.has(p.name) });

/* a site with a login: what it asks (a window, a login) and the way to log in */
function loginRow(p) {
  const asks = [p.needs_headed && "会弹出一个浏览器窗口", !p.logged_in && (p.auth === "login" ? "要先登录才能使用" : `登录后：${gives(loginOf(p.name)?.unlocks)}`)];
  const hint = asks.filter(Boolean).join("；");
  const end = p.logged_in ? pill("已登录", "ok") : btn("去登录", "login", { k: p.name });
  return usable(p) ? pickRow(p, hint, end) : itemRow({ name: p.label, hint, end, off: true });   // login required first: cannot be ticked yet
}

const SITE_ROW = {
  login: loginRow,
  window: (p) => (loginOf(p.name) ? loginRow(p) : pickRow(p, "会弹出一个浏览器窗口")),
  city: (p) => pickRow(p, "要先在上面填好城市"),
  keys: (p) => itemRow({ name: p.label, hint: "还没配置密钥", end: btn("如何配置", "how-keys"), off: true }),
};

function moreSites(g) {
  return GROUPS.filter(([k]) => g[k].length)
    .map(([k, title]) => `<div class="group"><h3>${title}</h3><div class="items">${g[k].map(SITE_ROW[k]).join("")}</div></div>`).join("");
}

/* a common site is one tap to (de)select; one that gets better with a login says so, with the way there */
function commonChip(p) {
  const picker = chip(p.label, S.picked.has(p.name), act("pick", { k: p.name }));
  if (p.auth !== "optional" || p.logged_in !== false) return picker;
  return `<span class="pick">${picker}<a class="soft" href="#/logins" aria-label="${esc(p.label)}：登录后更全，去平台登录">登录后更全</a></span>`;
}

const pickedCount = () => S.providers.filter((p) => S.picked.has(p.name) && usable(p)).length;

function pickerBody() {
  const g = siteGroups();
  const more = moreSites(g);
  return `<h2 class="label">常用网站<small>已经选好了，点一下可以取消</small></h2><div class="chips">${g.common.map(commonChip).join("")}</div>${more ? disclosure("more", "更多网站", more) : ""}`;
}

/* Once a search exists the picker shrinks to one line, so the search card stays in sight; a click opens it. */
const sitePicker = () => (S.searched ? disclosure("picker", `已选 ${pickedCount()} 个网站 · 修改`, pickerBody()) : pickerBody());

/* ---- first-time hint ---- */
function loginHint() {
  const todo = S.logins.filter(wantsLogin).length;
  return todo ? `<div class="notice">${icon("shield", 18)}<span>登录 ${todo} 个平台后，结果更全</span>${linkBtn("去登录", "#/logins", "line sm")}</div>` : "";
}

/* ---- searches ---- */
function searchState(s) {
  if (s.status === "running") return ["查找中", "", true];
  if (s.status === "waiting_login") return ["等你登录", "warn"];
  if (s.per_site.every((x) => x.status === "dead")) return ["没成功", "bad"];
  // a search that is "retrying" (nothing works, a site owes a retry) ends up here like "done": nothing found yet and
  // every site still owing a retry says so, rather than 已完成
  return !wholeJobs(s) && s.per_site.every((x) => x.outcome === "retrying" || x.status === "dead") ? ["稍后重试", "warn"] : ["已完成", "ok"];
}

/* a site's number is the jobs that are whole; "/ n" says how many it found in all */
function siteTail(x) {
  if (!x.jobs) return x.status === "done" ? '<span class="none">没有找到</span>' : "";
  const all = x.jobs > x.complete ? `<span class="none" title="信息完整 ${x.complete} 个，一共找到 ${x.jobs} 个"> / ${x.jobs}</span>` : "";
  return `<b>${x.complete}</b>${all}`;
}

function siteDot(x) {
  const [ico, tone] = x.status === "done" && !x.jobs ? ["dash", ""] : SITE_ICON[x.status];
  return `<li class="dot ${tone}">${ico === "spin" ? spinner() : icon(ico)}<span>${esc(x.label)}</span>${siteTail(x)}</li>`;
}

function searchCard(s) {
  const [text, tone, busy] = searchState(s);
  const done = s.per_site.filter((x) => x.status === "done" || x.status === "dead").length;
  return `<article class="card search-card">
    <div class="row"><h3>${esc(searchTitle(s))}</h3>
      <div class="actions">${pill(text, tone, busy ? spinner() : "")}${linkBtn("查看结果", resultsHref(s), "line sm", `查看结果：${searchTitle(s)}`)}</div></div>
    ${progress(done, s.per_site.length, busy)}
    <p class="sub small">已完成 ${done} / ${s.per_site.length} 个网站 · 已找到 ${wholeJobs(s)} 个岗位</p>
    <ul class="dots">${s.per_site.map(siteDot).join("")}</ul>${notesHtml(s)}</article>`;
}

const FIRST_RUN = '<p class="sub first-run">还没有查找记录。填好上面的岗位，点“开始查找”就行。</p>';

/* the newest search is shown under the search box; the others are listed here */
function earlierHtml(searches) {
  if (!searches.length) return `<h2 class="label">最近查找</h2><div class="grid2">${FIRST_RUN}</div>`;
  return searches.length > 1 ? `<h2 class="label">更早的查找</h2><div class="grid2">${searches.slice(1).map(searchCard).join("")}</div>` : "";
}

function paintSearches(searches) {
  S.searched = searches.length > 0;
  paint($("#newest"), S.searched ? searchCard(searches[0]) : "");
  paint($("#sites"), sitePicker());
  paint($("#recent"), earlierHtml(searches));
  return searches.some((s) => s.status === "running");   // "retrying" is not polled: the retry comes by itself
}

async function refreshSearches(e) {
  const { searches } = await api("GET /api/searches", { limit: 6 });
  return e === epoch && paintSearches(searches);
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
  <div id="newest"></div><div id="login-hint"></div><div id="sites"></div><div id="recent"></div></section>`;

async function viewSearch(params, e) {
  const g = siteGroups();
  S.picked ||= new Set([...g.common, ...g.city].map((p) => p.name));
  const { searches } = await api("GET /api/searches", { limit: 6 });
  if (e !== epoch) return;
  $("#view").innerHTML = searchPage();
  $("#kw").value = S.form.keyword;
  $("#city").value = S.form.city;
  paint($("#login-hint"), loginHint());
  $("#kw").focus();
  if (paintSearches(searches)) pollWhile(refreshSearches);
}

/* Without a city the sites that cannot search the whole country are left out of the request, quietly: the search card
   lists the sites that were searched. */
const searchable = (picked, city) => (city ? picked : picked.filter((p) => !p.requires_city));

/* the city message only comes when no site is left to search */
function searchProblem(keyword, picked, sites) {
  if (!keyword) return "请先填写岗位关键词";
  if (!picked.length) return "请至少选择一个网站";
  return sites.length ? "" : `${picked.map((p) => p.label).join("、")}需要先填写城市`;
}

/* "58同城、赶集网不支持“三亚”；鱼泡直聘还没配置密钥，这次没有找它们": the sites the search left out, grouped by why */
function skippedNote(skipped) {
  const byReason = new Map();
  for (const { label, reason } of skipped) byReason.set(reason, [...(byReason.get(reason) || []), label]);
  const why = [...byReason].map(([reason, labels]) => `${labels.join("、")}${reason}`);
  return `${why.join("；")}，这次没有找${skipped.length > 1 ? "它们" : "它"}`;
}

async function startSearch(form) {
  const keyword = $("#kw").value.trim();
  const city = $("#city").value.trim();
  const picked = S.providers.filter((p) => S.picked.has(p.name) && usable(p));
  const sites = searchable(picked, city);
  const problem = searchProblem(keyword, picked, sites);
  $("#msg").textContent = problem;
  if (problem) {
    if (!keyword) $("#kw").focus();
    return;
  }
  const go = form.querySelector("[type=submit]");
  go.disabled = true;
  try {
    const { skipped } = await api("POST /api/crawl", { keyword, city, providers: sites.map((p) => p.name) });
    if (skipped.length) toast(skippedNote(skipped));
    if (await refreshSearches(epoch)) pollWhile(refreshSearches);
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
