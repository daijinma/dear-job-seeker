/* 设置: speed, accounts, the advanced block (proxy, keys) and a few facts about this installation. */

const PRESETS = [["steady", "稳妥", "推荐 · 慢一点，更安全"], ["standard", "标准", "速度和安全折中"], ["fast", "较快", "可能被网站暂时限制"]];

function speedCard() {
  const now = S.prefs.preset || "steady";
  const opts = PRESETS.map(([k, name, hint]) =>
    `<button type="button" class="opt${now === k ? " on" : ""}" aria-pressed="${now === k}" ${act("preset", { k })}><b>${name}</b><small>${hint}</small></button>`);
  return `<div class="card"><h2>查找速度</h2><p class="sub">越快，越容易被招聘网站暂时限制访问。不确定就选“稳妥”。</p><div class="opts">${opts.join("")}</div></div>`;
}

function accountRow(p) {
  const end = p.logged_in
    ? pill("已登录", "ok") + btn("退出登录", "logout", { k: p.name })
    : pill("未登录", "warn") + btn("去登录", "login", { k: p.name }, "primary sm");
  return itemRow({ name: p.label, hint: LOGIN_HINT[p.auth], end });
}

function accountsCard() {
  const sites = S.providers.filter((p) => p.auth !== "public");
  if (!sites.length) return "";
  return `<div class="card"><h2>账号</h2><p class="sub">有些网站登录后能看到更多内容，比如薪资。登录只在你自己的电脑上完成，账号信息不会上传到任何地方。</p>
    <div class="items">${sites.map(accountRow).join("")}</div></div>`;
}

function proxyBlock(egress) {
  const proxies = egress.filter((e) => e.id !== "direct");
  const on = S.proxyOn || proxies.length > 0;
  const rows = proxies.map((e) => itemRow({ name: e.id, end: btn("删除", "del-proxy", { url: e.id }) })).join("");
  const form = `<div class="items">${rows}</div>
    <form class="add-proxy" data-submit="add-proxy">
      <input id="proxy-url" aria-label="代理服务器地址" placeholder="http://用户名:密码@地址:端口" autocomplete="off">
      <button type="submit" class="btn primary">添加</button></form>`;
  return `<section class="block"><div class="row"><div><h3>使用代理服务器</h3><p class="sub">一般不需要改。只有你自己有代理服务器时才用。</p></div>
    <button type="button" class="switch${on ? " on" : ""}" role="switch" aria-checked="${on}" aria-label="使用代理服务器"${proxies.length ? " disabled" : ""}
      ${act("proxy-toggle")}></button></div>${on ? form : ""}</section>`;
}

function keysBlock() {
  const sites = S.providers.filter((p) => p.requires_env.length);
  const rows = sites.map((p) => itemRow({ name: p.label, hint: p.requires_env.join("、"), end: pill(p.configured ? "已配置" : "未配置", p.configured ? "ok" : "warn") }));
  return `<section class="block" id="keys"><h3>需要密钥的网站</h3>
    <p class="sub">有些网站需要你自己的密钥才能查找，密钥只保存在这台电脑上。把它们写进配置文件夹里的 .env 文件（每行“名称=内容”），重启本程序就生效。</p>
    <div class="items">${rows.join("")}</div>${btn("打开配置文件夹", "open-home", {}, "line")}</section>`;
}

const advancedCard = (egress) => `<div class="card">${disclosure("advanced", "高级设置", proxyBlock(egress) + keysBlock(), "一般不需要改")}</div>`;
const aboutCard = (about) => `<div class="card"><h2>关于</h2><div class="items">
  ${itemRow({ name: "数据保存位置", hint: about.home })}${itemRow({ name: "版本", hint: about.version })}</div></div>`;

async function viewSettings(params, e) {
  const [{ egress }, about] = await Promise.all([api("GET /api/egress"), api("GET /api/about")]);
  if (e !== epoch) return;
  const keys = params.get("focus") === "keys";
  if (keys) S.open.advanced = true;
  $("#view").innerHTML = `<section class="narrow settings"><h1>设置</h1>${speedCard()}${accountsCard()}${advancedCard(egress)}${aboutCard(about)}</section>`;
  if (keys) $("#keys").scrollIntoView({ block: "center" });
}

Object.assign(ACTIONS, {
  preset: async (el) => {
    await api("POST /api/frequency_preset", { preset: el.dataset.k });
    toast(`已切换为“${PRESETS.find(([k]) => k === el.dataset.k)[1]}”`, "ok");
    await route();
  },
  logout: async (el) => {
    await api("POST /api/logout", { provider: el.dataset.k });
    toast("已退出登录");
    await route();
  },
  "proxy-toggle": () => { S.proxyOn = !S.proxyOn; return route(); },
  "add-proxy": async () => {
    await api("POST /api/egress", { url: $("#proxy-url").value });
    toast("已添加", "ok");
    await route();
  },
  "del-proxy": async (el) => {
    await api("DELETE /api/egress", { url: el.dataset.url });
    toast("已删除");
    await route();
  },
  "open-home": () => api("POST /api/open_home"),
});
