/* 我的结果: pick a search, filter and sort its jobs, read them as cards, open one in the drawer, export a table.
   Job text comes from third-party sites: it only ever enters the page through esc() or textContent. */

const PAGE = 30;
const SORT_OPTIONS = [["new", "最新发现"], ["company", "公司"]];
const CSV_HEAD = ["岗位", "公司", "薪资", "城市", "网站", "链接"];
let R = null;   // the open results page: { search, searches, provider, sort, jobs, total, perSite, searching, state, error, desc, open }

const sameSearch = (a, b) => a.keyword === b.keyword && (a.city || "") === (b.city || "");
const allCount = () => R.perSite.reduce((n, x) => n + x.count, 0);
const jobQuery = (extra = {}) => ({ keyword: R.search.keyword, city: R.search.city || "", provider: R.provider, sort: R.sort, ...extra });

function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "刚刚发现";
  const [n, unit] = s < 3600 ? [s / 60, "分钟"] : s < 86400 ? [s / 3600, "小时"] : [s / 86400, "天"];
  return `${Math.floor(n)} ${unit}前发现`;
}

/* ---- pieces of a job, shared by the card and the drawer ---- */
const jobTitle = (j) => esc(j.title || "（没有标题）");
const jobCompany = (j) => esc(j.company || "公司未公开");
/* The one place a salary becomes text: today the platform's own wording, later it can be built from structured fields. */
const salaryLabel = (j) => j.salary_text || "";
const jobPay = (j, cls = "") => (salaryLabel(j) ? `<div class="salary ${cls}">${esc(salaryLabel(j))}</div>` : '<div class="salary none">薪资未公开</div>');
const jobTags = (j) => `<div class="tags">${j.city ? pill(j.city, "gray") : ""}${pill(j.label)}<span class="when">${ago(j.first_seen_at)}</span></div>`;

function jobCard(j) {
  return `<article class="card job"><h3><button type="button" class="job-open" ${act("job", { id: j.id })}>${jobTitle(j)}</button></h3>
    <p class="sub">${jobCompany(j)}</p>${jobPay(j)}${jobTags(j)}</article>`;
}

/* ---- page regions ---- */
function stripChip(s) {
  const on = sameSearch(s, R.search);
  const label = `${esc(searchTitle(s))} · ${s.jobs_total} 个`;
  return `<a class="chip${on ? " on" : ""}" href="${esc(resultsHref(s))}"${on ? ' aria-current="true"' : ""}>${on ? icon("check") : ""}${label}</a>`;
}

function toolsHtml() {
  if (!R.jobs.length) return "";
  const sorts = SORT_OPTIONS.map(([k, text]) => chip(text, R.sort === k, act("sort", { k }), { sm: true })).join("");
  return `<div class="seg" role="group" aria-label="排序方式"><span class="sub">排序</span>${sorts}</div>${btn("导出表格", "export", {}, "primary")}`;
}

function noticeHtml() {
  const busy = R.searching ? `<div class="notice">${spinner()}<span>还在查找中，已找到 ${allCount()} 个，结果会自动更新</span></div>` : "";
  return busy + (R.jobs.length ? notesHtml(R.search) : "");
}

function filtersHtml() {
  if (R.perSite.length < 2) return "";
  const one = (k, text, count) => chip(text, (R.provider || "all") === k, act("site", { k }), { count });
  return one("all", "全部", allCount()) + R.perSite.map((x) => one(x.provider, x.label, x.count)).join("");
}

function noJobs() {
  return `<div class="card empty"><h2>这次没有找到岗位</h2>
    <ul class="tips"><li>放宽关键词，比如把“高级前端工程师”改成“前端”</li><li>去掉城市，看看全国的结果</li><li>换几个网站再找一次</li></ul>
    ${notesHtml(R.search, true)}${linkBtn("回到查找", "#/search", "primary")}</div>`;
}

function listHtml() {
  if (R.state === "error") return emptyState("没能读取结果", R.error, btn("重试", "reload", {}, "primary"));
  if (R.jobs.length) return `<div class="jobs">${R.jobs.map(jobCard).join("")}</div>`;
  return R.state === "loading" || R.searching ? `<div class="jobs">${skeletons(6)}</div>` : noJobs();
}

function moreHtml() {
  if (R.state !== "ready" || !R.jobs.length) return "";
  const shown = `<span class="sub">已显示 ${R.jobs.length} / ${R.total} 个</span>`;
  const next = `<button type="button" class="btn line" ${R.loadingMore ? "disabled" : act("more")}>${R.loadingMore ? "加载中…" : "加载更多"}</button>`;
  return shown + (R.jobs.length < R.total ? next : "");
}

function paintResults() {
  paint($("#strip"), R.searches.map(stripChip).join(""));
  $("#found").textContent = R.state === "ready" ? `找到 ${allCount()} 个岗位` : "我的结果";
  $("#what").textContent = searchTitle(R.search);
  paint($("#tools"), toolsHtml());
  paint($("#notice"), noticeHtml());
  paint($("#filters"), filtersHtml());
  paint($("#list"), listHtml());
  paint($("#more"), moreHtml());
}

const resultsFrame = () => `<section class="results">
  <nav class="strip" id="strip" aria-label="最近的查找"></nav>
  <div class="head"><div><h1 id="found"></h1><p class="sub" id="what"></p></div><div class="tools" id="tools"></div></div>
  <div id="notice"></div><div class="chips" id="filters" role="group" aria-label="按网站筛选"></div>
  <div id="list"></div><div class="more-row" id="more"></div></section>`;

/* ---- loading ---- */
function takePage(d, append) {
  const have = new Set(R.jobs.map((j) => j.id));
  R.jobs = append ? [...R.jobs, ...d.jobs.filter((j) => !have.has(j.id))] : d.jobs;
  Object.assign(R, { total: d.total, perSite: d.per_site, searching: d.searching, state: "ready" });
}

/* the first page (view start, filter, sort): a failure here becomes the error state with a retry button */
async function showJobs(extra) {
  const e = epoch;
  try {
    const d = await api("GET /api/search_jobs", jobQuery(extra));
    if (e !== epoch) return;
    takePage(d, false);
  } catch (err) {
    if (e !== epoch) return;
    if (err.bridge) throw err;
    Object.assign(R, { state: "error", error: err.message });
  }
  paintResults();
}

async function loadMore() {
  const e = epoch;
  R.loadingMore = true;
  paintResults();
  try {
    const d = await api("GET /api/search_jobs", jobQuery({ limit: PAGE, offset: R.jobs.length }));
    if (e === epoch) takePage(d, true);
  } finally {
    if (e === epoch) {
      R.loadingMore = false;
      paintResults();
    }
  }
}

/* while the search runs: re-read what is loaded so far (same rows, plus whatever is new) and the per-site status */
async function refreshResults(e) {
  const [{ searches }, d] = await Promise.all([
    api("GET /api/searches", { limit: 20 }),
    api("GET /api/search_jobs", jobQuery({ limit: Math.max(PAGE, R.jobs.length) })),
  ]);
  if (e !== epoch) return false;
  R.searches = searches;
  R.search = searches.find((s) => sameSearch(s, R.search)) || R.search;
  takePage(d, false);
  paintResults();
  return d.searching;
}

async function viewResults(params, e) {
  $("#view").innerHTML = loading("正在读取你的查找记录…");
  const { searches } = await api("GET /api/searches", { limit: 20 });
  if (e !== epoch) return;
  if (!searches.length) {
    $("#view").innerHTML = emptyState("还没有查找记录", "先去“查找”页告诉我想找什么工作，找到的岗位会放在这里。", linkBtn("去查找", "#/search", "primary"));
    return;
  }
  const wanted = { keyword: params.get("k"), city: params.get("c") || "" };
  R = {
    search: searches.find((s) => sameSearch(s, wanted)) || searches[0], searches, provider: null, sort: "new",
    jobs: [], total: 0, perSite: [], searching: false, state: "loading", desc: new Map(), open: null,
  };
  $("#view").innerHTML = resultsFrame();
  paintResults();
  $("#strip .on")?.scrollIntoView({ block: "nearest", inline: "center" });
  await showJobs();
  if (R.searching) pollWhile(refreshResults);
}

/* ---- the detail drawer ---- */
function descStatus(d, job) {
  if (d.state === "loading") return loading(job.has_description ? "正在读取…" : "正在到网站上获取这个岗位的详细介绍，可能要几秒钟…");
  if (d.state === "error") return `<div class="note bad">${icon("alert", 16)}<span>${esc(d.error)}</span>${btn("重试", "desc-retry")}</div>`;
  return d.text ? "" : '<p class="sub">这个岗位没有提供更详细的介绍，可以点下面的“打开原页面”去看看。</p>';
}

function paintDescription() {
  const d = R.desc.get(R.open.id) || { state: "loading" };
  const text = $("#drawer-text");
  text.textContent = d.state === "ok" ? d.text : "";   // textContent: the text can never become markup
  text.hidden = !(d.state === "ok" && d.text);
  paint($("#drawer-status"), descStatus(d, R.open));
}

function drawerHtml(j) {
  const open = j.source_url ? btn("打开原页面", "open-source", {}, "primary") : "";
  return `<aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
    <div class="drawer-head"><button type="button" class="icon-btn" data-close ${act("close-drawer")} aria-label="关闭">${icon("x", 18)}</button></div>
    <div class="drawer-body"><h2 id="drawer-title">${jobTitle(j)}</h2><p class="sub">${jobCompany(j)}</p>
      ${jobPay(j, "big")}${jobTags(j)}<h3 class="label">岗位介绍</h3>
      <div id="drawer-status"></div><div id="drawer-text" class="prose" hidden></div></div>
    <div class="drawer-foot">${open}</div></aside>`;
}

async function loadDescription(job) {
  if (R.desc.get(job.id)?.state !== "ok") {
    R.desc.set(job.id, { state: "loading" });
    paintDescription();
    try {
      const { job: got } = await api("POST /api/job_detail", { job_id: job.id });
      R.desc.set(job.id, { state: "ok", text: got.description });
    } catch (e) {
      R.desc.set(job.id, { state: "error", error: e.message });
    }
  }
  if (R.open?.id === job.id) paintDescription();
}

function openJob(id) {
  R.open = R.jobs.find((j) => j.id === id);
  showLayer("drawer", drawerHtml(R.open));
  loadDescription(R.open);   // not awaited: the drawer is already up and shows its own loading state
}

/* ---- export: every job of this search and filter, not only the loaded page ---- */
async function allJobs() {
  const rows = [];
  for (;;) {
    const d = await api("GET /api/search_jobs", jobQuery({ limit: 1000, offset: rows.length }));
    rows.push(...d.jobs);
    if (!d.jobs.length || rows.length >= d.total) return rows;
  }
}

const csvCell = (v) => `"${String(v ?? "").replace(/^[=+\-@\t\r]/, "'$&").replace(/"/g, '""')}"`;   // a leading = + - @ would run as a formula in a spreadsheet
const csvRow = (j) => [j.title, j.company, salaryLabel(j), j.city, j.label, j.source_url];
const csvText = (rows) => "\ufeff" + [CSV_HEAD, ...rows.map(csvRow)].map((r) => r.map(csvCell).join(",")).join("\r\n");

async function exportCsv(el) {
  el.disabled = true;
  try {
    const name = `${R.search.keyword}${R.search.city ? "-" + R.search.city : ""}`.replace(/[\\/:*?"<>|]/g, "_");
    const saved = await window.pywebview.api.save_csv(`${name}.csv`, csvText(await allJobs()));
    if (saved.error) throw new Error(saved.error);
    if (saved.saved) toast(`已保存到：${saved.saved}`, "ok");
  } finally {
    el.disabled = false;
  }
}

Object.assign(ACTIONS, {
  job: (el) => openJob(Number(el.dataset.id)),
  "close-drawer": () => { R.open = null; hideLayer("drawer"); },
  "desc-retry": () => { R.desc.delete(R.open.id); return loadDescription(R.open); },
  "open-source": () => api("POST /api/open_url", { url: R.open.source_url }),
  site: (el) => { R.provider = el.dataset.k === "all" ? null : el.dataset.k; return showJobs(); },
  sort: (el) => { R.sort = el.dataset.k; return showJobs(); },
  more: loadMore,
  export: exportCsv,
});
