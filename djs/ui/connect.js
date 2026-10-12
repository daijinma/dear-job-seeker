/* 连接 AI 助手: the card on 设置 (one switch per assistant found on this computer) and the one-time invitation on 查找.
   Every /api/connect route answers in one shape: {command, asked, config, targets: [{key, label, group, detected,
   state, path}], guided: [{key, label, where}]}; connect_many adds `failed: [{label, error}]`. */

const CONNECT_STATE = { off: "未连接", connected: "已连接", stale: "需要更新", unreadable: "配置文件读不懂，请用复制配置" };
const CONNECT_GROUPS = [["cn", "国内应用"], ["intl", "其他"]];   // the order the card lists them in
const invite = { data: null, ticked: new Set() };   // the answer the invitation was made from, and the keys ticked in it

/* ---- 设置 ---- */
function connectRow(t) {
  const unreadable = t.state === "unreadable";
  const end = (t.state === "stale" ? btn("更新", "connect-update", { k: t.key }) : "") + (unreadable ? btn("复制配置", "connect-copy") : "")
    + switchBtn(t.state === "connected" || t.state === "stale", `连接 ${t.label}`, act("connect-toggle", { k: t.key }), unreadable);
  return itemRow({ name: t.label, hint: `${CONNECT_STATE[t.state]} · ${t.path}`, end });
}

function connectCard(data) {
  const found = data.targets.filter((t) => t.detected);
  const groups = CONNECT_GROUPS.map(([g, title]) => [title, found.filter((t) => t.group === g)]).filter(([, rows]) => rows.length)
    .map(([title, rows]) => `<div class="group"><h3>${title}</h3><div class="items">${rows.map(connectRow).join("")}</div></div>`);
  const missing = data.targets.filter((t) => !t.detected);
  const guided = data.guided.map((g) => itemRow({ name: g.label, hint: g.where, end: btn("复制配置", "connect-copy") }));
  return `<div class="card connect" id="connect-card" data-config="${esc(data.config)}"><h2>连接 AI 助手</h2>
    <p class="sub">让你电脑上的 AI 助手直接用本程序查岗位，不用自己改配置文件。</p>
    ${groups.join("") || '<p class="sub">这台电脑上还没有找到 AI 助手。</p>'}
    ${missing.length ? `<div>${disclosure("connect-missing", "没检测到的应用", `<div class="chips">${missing.map((t) => pill(t.label, "gray")).join("")}</div>`)}</div>` : ""}
    <div>${disclosure("connect-guided", "手动添加（没有公开配置文件的应用）", `<div class="items">${guided.join("")}</div>`)}</div></div>`;
}

/* the clipboard API where the window allows it; a hidden text box and the old copy command where it does not (the
   box sits in view, so selecting it does not scroll the page, and the keyboard goes back to the button it was on) */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const was = document.activeElement;
    const box = Object.assign(document.createElement("textarea"), { value: text, readOnly: true });
    box.style.cssText = "position:fixed;top:0;left:0;opacity:0";
    document.body.append(box);
    box.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { /* the toast says it did not work */ }
    box.remove();
    was?.focus?.({ preventScroll: true });
    return ok;
  }
}

/* switch a target on or off (or bring a stale entry up to date); the card shows what the route answered */
async function connectSet(el, on, done = "已连接") {
  const e = epoch;
  const key = el.dataset.k;
  el.disabled = true;
  try {
    const data = await api("POST /api/connect", { key, on });
    if (e !== epoch) return;
    $("#connect-card").outerHTML = connectCard(data);
    $(`#connect-card [data-act="connect-toggle"][data-k="${key}"]`)?.focus();   // the repaint replaced the button the keyboard was on
    const label = data.targets.find((t) => t.key === key).label;
    toast(on ? `${done} ${label}，重启它后生效` : `已断开 ${label}`, "ok");
  } finally {
    el.disabled = false;
  }
}

/* ---- 查找 ---- */
function inviteRow(t) {
  if (t.state === "unreadable") return itemRow({ name: t.label, hint: "配置文件读不懂，请到设置里复制配置", off: true });
  return itemRow({ name: t.label, hint: t.state === "connected" ? "已连接" : t.path, pick: act("connect-pick", { k: t.key }), on: invite.ticked.has(t.key) });
}

const inviteHtml = () => `<div class="card connect-ask"><h2>把“找岗位助手”接入你电脑上的 AI 助手？</h2>
  <p class="sub">勾上的 AI 助手接入后，就能直接用本程序查岗位。只会加一条配置，其他内容不动，原来的配置文件会先备份。以后可以在“设置”里随时关掉。</p>
  <div class="items">${invite.data.targets.filter((t) => t.detected).map(inviteRow).join("")}</div>
  <div class="actions">${btn("以后再说", "connect-later", {}, "line")}${btn("接入", "connect-go", {}, "primary")}</div></div>`;

/* the invitation, or "" when it was answered before or there is nothing on this computer to offer it for (nothing
   found, nothing we can read, or everything found is connected already) */
function connectAsk(data) {
  const open = data.targets.filter((t) => t.detected && t.state !== "unreadable");
  if (data.asked || !open.some((t) => t.state !== "connected")) return "";
  Object.assign(invite, { data, ticked: new Set(open.map((t) => t.key)) });
  return inviteHtml();
}

/* The invitation fills its own place once the answer is in: the search form never waits for it, and a failure to ask
   is not worth a message (the same list is on 设置). */
function showConnectAsk(e) {
  api("GET /api/connect").then((data) => { if (e === epoch) paint($("#connect-ask"), connectAsk(data)); }, () => {});
}

/* "已接入 2 个；没接入成功：Cursor（…）" */
function connectResult(done, failed) {
  const bad = failed.map((f) => `${f.label}（${f.error}）`).join("；");
  if (!bad) return [`已接入 ${done} 个 AI 助手，重启这些应用后生效`, "ok"];
  return [`${done ? `已接入 ${done} 个；` : ""}没接入成功：${bad}`, "bad", { label: "去设置", name: "connect-settings" }];
}

Object.assign(ACTIONS, {
  "connect-toggle": (el) => connectSet(el, el.getAttribute("aria-checked") !== "true"),
  "connect-update": (el) => connectSet(el, true, "已更新"),
  "connect-copy": async (el) => {
    const ok = await copyText(el.closest("[data-config]").dataset.config);
    toast(ok ? "已复制" : "没能复制到剪贴板，请再试一次", ok ? "ok" : "bad");
  },
  "connect-pick": (el) => {
    const k = el.dataset.k;
    invite.ticked.has(k) ? invite.ticked.delete(k) : invite.ticked.add(k);
    paint($("#connect-ask"), inviteHtml());
    $(`#connect-ask [data-act="connect-pick"][data-k="${k}"]`)?.focus();
  },
  "connect-go": async (el) => {
    const keys = invite.data.targets.filter((t) => invite.ticked.has(t.key)).map((t) => t.key);
    if (!keys.length) return toast("请先勾选要接入的 AI 助手");
    const e = epoch;
    el.disabled = true;
    try {
      const { failed } = await api("POST /api/connect_many", { keys });
      if (e === epoch) paint($("#connect-ask"), "");
      toast(...connectResult(keys.length - failed.length, failed));
    } finally {
      el.disabled = false;
    }
  },
  "connect-later": async () => {
    const e = epoch;
    await api("POST /api/connect_asked");
    if (e === epoch) paint($("#connect-ask"), "");
    toast("好的，想接入时到“设置”里就行");
  },
  "connect-settings": () => { location.hash = "#/settings"; },
});
