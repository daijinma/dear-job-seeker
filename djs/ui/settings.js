/* 设置: speed, the way to the logins, the AI assistants (connect.js), the advanced block (proxy, keys), a few facts
   about this installation and the software update (update.js). */

const PRESETS = [["steady", "稳妥", "推荐 · 慢一点，更安全"], ["standard", "标准", "速度和安全折中"], ["fast", "较快", "可能被网站暂时限制"]];

function speedCard() {
  const now = S.prefs.preset || "steady";
  const opts = PRESETS.map(([k, name, hint]) =>
    `<button type="button" class="opt${now === k ? " on" : ""}" aria-pressed="${now === k}" ${act("preset", { k })}><b>${name}</b><small>${hint}</small></button>`);
  return `<div class="card"><h2>查找速度</h2><p class="sub">越快，越容易被招聘网站暂时限制访问。不确定就选“稳妥”。</p><div class="opts">${opts.join("")}</div></div>`;
}

/* the accounts live on the 平台登录 page; here is only the way there */
const loginsCard = () => (S.logins.length
  ? `<div class="card row"><div><h2>平台登录</h2><p class="sub">登录后，有些网站能看到更多、更完整的岗位。</p></div>${linkBtn("去平台登录", "#/logins", "primary")}</div>` : "");

function proxyBlock(egress) {
  const proxies = egress.filter((e) => e.id !== "direct");
  const on = S.proxyOn || proxies.length > 0;
  const rows = proxies.map((e) => itemRow({ name: e.id, end: btn("删除", "del-proxy", { url: e.id }) })).join("");
  const form = `<div class="items">${rows}</div>
    <form class="add-proxy" data-submit="add-proxy">
      <input id="proxy-url" aria-label="代理服务器地址" placeholder="http://用户名:密码@地址:端口" autocomplete="off">
      <button type="submit" class="btn primary">添加</button></form>`;
  return `<section class="block"><div class="row"><div><h3>使用代理服务器</h3><p class="sub">一般不需要改。只有你自己有代理服务器时才用。</p></div>
    ${switchBtn(on, "使用代理服务器", act("proxy-toggle"), proxies.length > 0)}</div>${on ? form : ""}</section>`;
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
  const [{ egress }, about, connect, update] = await Promise.all([api("GET /api/egress"), api("GET /api/about"), api("GET /api/connect"),
    readUpdate().catch(() => S.update)]);   // the card is left out when the state cannot be read
  if (e !== epoch) return;
  const keys = params.get("focus") === "keys";
  if (keys) S.open.advanced = true;
  $("#view").innerHTML = `<section class="narrow settings"><h1>设置</h1>${speedCard()}${loginsCard()}${connectCard(connect)}${advancedCard(egress)}${aboutCard(about)}${updateSlot(update)}</section>`;
  paintUpdateCard();
  if (keys) $("#keys").scrollIntoView({ block: "center" });
}

Object.assign(ACTIONS, {
  preset: async (el) => {
    await api("POST /api/frequency_preset", { preset: el.dataset.k });
    toast(`已切换为“${PRESETS.find(([k]) => k === el.dataset.k)[1]}”`, "ok");
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
