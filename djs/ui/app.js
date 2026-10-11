const SITES = {};  // name -> display label, filled from GET /api/providers
const STATUS = { pending: ["排队中", "gray"], running: ["进行中", ""], waiting_login: ["等待登录", "warn"], done: ["完成", "ok"], dead: ["已放弃", "gray"] };
const PRESETS = [["steady", "稳妥", "推荐 · 慢一点，更安全"], ["standard", "标准", "速度和安全折中"], ["fast", "较快", "可能被网站暂时限制"]];
const CHECK = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="m5 12 5 5 9-10"/></svg>';
const S = { providers: [], tasks: [], preset: "steady", picked: null, site: "all", jobs: [], proxyOn: false };
let timer = null;

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
async function api(route, payload = {}) {
  const data = await window.pywebview.api.call(route, payload);
  if (data.error) throw new Error(data.error);
  return data;
}
const pill = (text, cls = "") => `<span class="pill ${cls}">${esc(text)}</span>`;
const loggedIn = () => S.providers.find((p) => p.name === "boss")?.logged_in;

async function refresh() {
  const [p, t] = await Promise.all([api("GET /api/providers"), api("GET /api/tasks")]);
  S.providers = p.providers;
  S.providers.forEach((x) => { SITES[x.name] = x.label; });
  S.picked ||= new Set(S.providers.filter((x) => x.auth === "public" && !x.requires_city).map((x) => x.name));
  S.preset = p.preset || "steady";
  S.tasks = t.tasks;
}

/* ---------- 查找 ---------- */
function renderChips() {
  $("#chips").innerHTML = Object.entries(SITES).map(([k, name]) => {
    if (k === "boss" && !loggedIn())
      return `<span class="chip off">${name} ${pill("需要先登录", "warn")}</span><a data-act="login">去登录</a>`;
    return `<button class="chip ${S.picked.has(k) ? "on" : ""}" data-act="pick" data-k="${k}">${S.picked.has(k) ? CHECK : ""}${name}</button>`;
  }).join("");
}

function groupTasks() {
  const groups = new Map();
  for (const t of S.tasks) {
    const key = `${t.payload.keyword}|${t.payload.city || ""}`;
    const g = groups.get(key) || { title: `${t.payload.keyword} · ${t.payload.city || "全国"}`, tasks: [] };
    g.tasks.push(t);
    groups.set(key, g);
  }
  return [...groups.values()].slice(0, 6);
}

function groupCard(g) {
  const has = (s) => g.tasks.some((t) => t.status === s);
  const status = ["running", "waiting_login", "pending", "done"].find(has) || "dead";
  const found = g.tasks.reduce((n, t) => n + Math.max(t.jobs_found || 0, t.stats ? t.stats.jobs_new + t.stats.jobs_seen : 0), 0);
  const line = status === "running" || status === "pending" ? `正在查找… 已找到 ${found} 个` : `已找到 ${found} 个岗位`;
  const [label, cls] = STATUS[status];
  return `<div class="card recent"><div class="row"><b>${esc(g.title)}</b>
    <span>${status === "waiting_login" ? '<a data-act="login">去登录</a> ' : ""}${pill(label, cls)}</span></div><div class="sub">${line}</div></div>`;
}

function renderRecent() {
  const groups = groupTasks();
  $("#recent").innerHTML = groups.length ? groups.map(groupCard).join("") : '<div class="sub">还没有查找记录</div>';
  const busy = S.tasks.some((t) => t.status === "pending" || t.status === "running");
  clearTimeout(timer);
  if (busy) timer = setTimeout(pollSearch, 3000);
}

async function pollSearch() {
  if (!$("#recent")) return;
  await refresh();
  renderChips();
  renderRecent();
}

async function startSearch() {
  const picked = [...S.picked].filter((k) => k !== "boss" || loggedIn());
  try {
    await api("POST /api/crawl", { keyword: $("#kw").value, city: $("#city").value, providers: picked });
    $("#msg").textContent = "";
    await pollSearch();
  } catch (e) { $("#msg").textContent = e.message; }
}

function viewSearch() {
  $("#view").innerHTML = `<div class="narrow"><div class="hero"><h1>想找什么工作？</h1>
    <p class="sub">告诉我岗位和城市，我帮你到几个招聘网站一起找。</p><div class="accent">let's go ✦</div></div>
    <div class="card searchbar"><label class="field"><span>岗位或关键词</span><input id="kw" placeholder="例如：前端工程师"></label>
    <label class="field"><span>城市</span><input id="city" placeholder="例如：杭州（不填=全国）"></label>
    <button class="btn primary" data-act="search">开始查找</button></div><div class="msg" id="msg"></div>
    <div class="label" style="margin-top:12px">到哪些网站找</div><div class="chips" id="chips"></div>
    <div class="label">最近查找</div><div class="grid" id="recent"></div></div>`;
  renderChips();
  pollSearch();
}

/* ---------- 我的结果 ---------- */
const salary = (j) => j.salary_text || "面议";
function jobCard(j) {
  return `<div class="card job"><h3>${esc(j.title)}</h3><div class="sub">${esc(j.company)}</div><div class="salary">${esc(salary(j))}</div>
    <div class="tags">${j.city ? pill(j.city, "gray") : ""}${pill(SITES[j.provider] || j.provider)}</div>
    ${j.source_url ? `<a data-act="open" data-url="${esc(j.source_url)}">查看详情 →</a>` : ""}</div>`;
}

const shown = () => S.jobs.filter((j) => S.site === "all" || j.provider === S.site);

function renderJobs() {
  const counts = S.jobs.reduce((m, j) => ({ ...m, [j.provider]: (m[j.provider] || 0) + 1 }), {});
  const chip = (k, name, n) => `<button class="chip ${S.site === k ? "on" : ""}" data-act="site" data-k="${k}">${S.site === k ? CHECK : ""}${name} ${n}</button>`;
  $("#sites").innerHTML = chip("all", "全部", S.jobs.length) + Object.keys(counts).map((k) => chip(k, SITES[k] || k, counts[k])).join("");
  $("#count").textContent = shown().length;
  $("#jobs").innerHTML = shown().length ? shown().map(jobCard).join("") : '<div class="card empty sub" style="grid-column:1/-1">还没有结果，去“查找”试试吧</div>';
}

async function exportCsv() {
  const cell = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const rows = [["岗位", "公司", "薪资", "城市", "网站", "链接"], ...shown().map((j) => [j.title, j.company, salary(j), j.city, SITES[j.provider] || j.provider, j.source_url])];
  const r = await window.pywebview.api.save_csv("岗位.csv", "\ufeff" + rows.map((r) => r.map(cell).join(",")).join("\r\n"));
  if (r.saved) alert("已保存到：" + r.saved);
  else if (r.error) alert(r.error);
}

async function viewResults() {
  S.site = "all";
  $("#view").innerHTML = `<div class="head"><div><h1>找到 <span id="count">0</span> 个岗位</h1><div class="sub">来自你最近的查找</div></div>
    <button class="btn line" data-act="export">导出表格</button></div><div class="chips" id="sites" style="margin-top:20px"></div><div class="jobs" id="jobs"></div>`;
  S.jobs = (await api("GET /api/jobs")).jobs;
  renderJobs();
}

/* ---------- 登录弹窗 ---------- */
function showModal(html) { $("#modal").innerHTML = `<div class="card modal">${html}</div>`; $("#modal").hidden = false; }
function closeModal() { $("#modal").hidden = true; }

function loginModal(state = "idle", error = "") {
  const steps = ["点下面的“打开登录页”", "打开 BOSS 直聘 App，扫一扫屏幕上的二维码", "登录成功后，这里会自动提示你"];
  const text = { running: "正在等待你扫码登录…（最长 3 分钟）", ok: "登录成功！等待中的查找会自动继续。", failed: error || "登录没有成功，请再试一次。" }[state];
  showModal(`<h2>登录 BOSS 直聘</h2><div class="sub">登录一次，以后查找就能带上你的账号。</div>
    <ul class="steps">${steps.map((s, i) => `<li><i>${i + 1}</i>${s}</li>`).join("")}</ul>
    <div class="note">${text || "登录只在你自己的电脑上完成，账号信息不会上传到任何地方。"}</div>
    <div class="actions"><button class="btn" data-act="close">${state === "ok" ? "关闭" : "以后再说"}</button>
    <button class="btn primary" data-act="open-login" ${state === "running" || state === "ok" ? "disabled" : ""}>打开登录页</button></div>`);
}

async function openLogin() {
  try { await api("POST /api/login", { provider: "boss" }); } catch (e) { return loginModal("failed", e.message); }
  loginModal("running");
  for (;;) {
    await new Promise((r) => setTimeout(r, 2000));
    const { login } = await api("GET /api/login_status", { provider: "boss" });
    if (login.state === "running") continue;
    loginModal(login.state, login.error);
    await route();
    return;
  }
}

/* ---------- 设置 ---------- */
async function viewSettings() {
  const { egress } = await api("GET /api/egress");
  const proxies = egress.filter((e) => e.id !== "direct").map((e) => `<div class="row"><span>${esc(e.id)}</span><a data-act="del-proxy" data-url="${esc(e.id)}">删除</a></div>`).join("");
  $("#view").innerHTML = `<div class="settings narrow"><h1>设置</h1>
    <div class="card"><h2>查找速度</h2><div class="sub">越快，越容易被招聘网站暂时限制访问。不确定就选“稳妥”。</div>
      <div class="opts">${PRESETS.map(([k, n, d]) => `<button class="opt ${S.preset === k ? "on" : ""}" data-act="preset" data-k="${k}"><b>${n}</b><small>${d}</small></button>`).join("")}</div></div>
    <div class="card"><h2>账号</h2><div class="sub">已登录的网站，查找时会带上你的账号，能看到更多岗位。</div>
      <div class="row"><b>BOSS 直聘</b><span>${loggedIn() ? pill("已登录", "ok") + ' <button class="btn line" data-act="logout">退出登录</button>'
        : pill("未登录", "warn") + ' <button class="btn primary" data-act="login">去登录</button>'}</span></div></div>
    <div class="card"><h2>网络线路（高级）</h2><div class="sub">一般不需要改。只有你自己有代理服务器时才用。</div>
      <div class="row"><span>使用代理服务器</span><button class="switch ${S.proxyOn ? "on" : ""}" data-act="proxy-toggle" aria-label="使用代理服务器"></button></div>
      ${S.proxyOn ? `<div class="proxy">${proxies}<div class="row"><input id="proxy-url" placeholder="http://用户名:密码@地址:端口"><button class="btn primary" data-act="add-proxy">添加</button></div><div class="msg" id="msg"></div></div>` : ""}</div></div>`;
}

/* ---------- 路由与事件 ---------- */
async function route() {
  clearTimeout(timer);
  const tab = (location.hash.match(/#\/(\w+)/) || [, "search"])[1];
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  await refresh();
  ({ search: viewSearch, results: viewResults, settings: viewSettings }[tab] || viewSearch)();
}

const ACTIONS = {
  search: startSearch,
  pick: (el) => { S.picked.has(el.dataset.k) ? S.picked.delete(el.dataset.k) : S.picked.add(el.dataset.k); renderChips(); },
  login: () => loginModal(),
  "open-login": openLogin,
  close: closeModal,
  site: (el) => { S.site = el.dataset.k; renderJobs(); },
  export: exportCsv,
  open: (el) => api("POST /api/open_url", { url: el.dataset.url }),
  preset: async (el) => { await api("POST /api/frequency_preset", { preset: el.dataset.k }); S.preset = el.dataset.k; viewSettings(); },
  logout: async () => { await api("POST /api/logout", { provider: "boss" }); await route(); },
  "proxy-toggle": () => { S.proxyOn = !S.proxyOn; viewSettings(); },
  "add-proxy": async () => {
    try { await api("POST /api/egress", { url: $("#proxy-url").value }); await viewSettings(); } catch (e) { $("#msg").textContent = e.message; }
  },
  "del-proxy": async (el) => { await api("DELETE /api/egress", { url: el.dataset.url }); viewSettings(); },
};

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (el) ACTIONS[el.dataset.act](el);
});
addEventListener("hashchange", route);
addEventListener("pywebviewready", route);
