/* 软件更新: Python checks for and downloads a new build by itself; the window only asks how it is going, every few seconds
   (it has no push channel), and speaks up when the build is ready. Two places show the state: the persistent message
   「更新已就绪」 at the lower right, and the 软件更新 card on 设置. Every read of GET /api/update goes through readUpdate,
   so a change is seen once, whoever read it. The routes answer {current, latest, state: none | downloading | ready |
   failed, note, install_on_quit, can_install, reason}; GET /api/update adds `result` (how the last install went) to
   the first answer after the app started. */

let UPDATE_POLL_MS = 5000;
let updateTimer = 0;

/* ---- reading the state ---- */
function seeUpdate(u) {
  S.update = u;
  if (u.result) toast(u.result);
  paintUpdateReady(u);
  paintUpdateCard();
  return u;
}

const readUpdate = async () => seeUpdate(await api("GET /api/update"));

/* once now and every UPDATE_POLL_MS after; a read that fails (the bridge says so by itself) is tried again next time */
function pollUpdate() {
  clearTimeout(updateTimer);
  readUpdate().catch(() => {}).finally(() => {
    clearTimeout(updateTimer);
    updateTimer = setTimeout(pollUpdate, UPDATE_POLL_MS);
  });
}

/* ---- the message: a downloaded build waits; it cannot be waved away, only answered ---- */
function paintUpdateReady(u) {
  const el = $("#update-ready");
  el.hidden = !(u.state === "ready" && !u.install_on_quit);
  const here = u.can_install !== false;   // where the app cannot be replaced in place the user is shown the new build instead
  paint(el, el.hidden ? "" : `<h2 id="update-ready-title">更新已就绪</h2><p>新版本已下载好，可以安装了。</p>
    ${here ? "" : `<p>${esc(u.reason)}</p>`}
    <div class="actions">${here ? `<button type="button" class="btn primary sm" ${act("update-install")}>安装并重启</button>
      ${btn("下次启动时安装", "update-later", {}, "line sm")}` : btn("打开下载好的位置", "update-reveal", {}, "primary sm")}</div>`);
}

/* ---- the card on 设置 ---- */
const updateSlot = (u) => (u ? '<div class="card" id="update-card"></div>' : "");

function updateButton(u) {
  if (!u.current) return ["检查更新", "update-check", "line", true];
  if (u.state === "downloading") return ["正在下载新版本…", "update-check", "line", true];
  if (u.state === "ready") return u.can_install === false ? ["打开下载好的位置", "update-reveal", "primary", false] : ["重启并安装更新", "update-install", "primary", false];
  return ["检查更新", "update-check", "line", false];
}

function updateCardBody(u) {
  const [text, name, cls, off] = updateButton(u);
  const ready = u.state === "ready";
  const notes = [!u.current && "开发版不会自动更新", u.state === "failed" && u.note, ready && u.can_install === false && u.reason,
                 ready && u.can_install !== false && "已连接的 AI 助手要重启一次才会用新版"].filter(Boolean);
  return `<div class="row"><div><h2>软件更新</h2><p class="sub">${u.current ? `当前版本 ${esc(u.current)}` : "开发版"}</p></div>
    <button type="button" class="btn ${cls}" ${act(name)}${off ? " disabled" : ""}>${esc(text)}</button></div>
    <div class="update-notes">${notes.map((t) => `<p class="sub">${esc(t)}</p>`).join("")}${btn("打开日志文件夹", "open-logs", {}, "link")}</div>`;
}

function paintUpdateCard() {
  const el = $("#update-card");
  if (el && S.update) paint(el, updateCardBody(S.update));
}

/* ---- what the buttons do ---- */
/* A manual check: a build that is on its way says so; nothing new says that; one that could not be asked says nothing
   (the log has it) */
async function checkUpdate(el) {
  const label = el.textContent;
  el.textContent = "正在检查…";
  el.disabled = true;
  try {
    const u = seeUpdate(await api("POST /api/update_check"));
    if (u.state === "downloading") toast("有新版本 · 正在下载新版本…", "", null, 3000);
    else if (!u.found && u.state !== "failed") toast("当前没有可用的更新");
  } finally {
    el.textContent = label;
    el.disabled = false;
  }
}

/* The window closes by itself a moment after the answer, and the new build starts. Searches that are running are handed
   back and go on after the restart; the user is told before they are interrupted. */
async function installUpdate(el) {
  let closing = false;
  el.disabled = true;
  try {
    const { searches } = await api("GET /api/searches", { limit: 20 });
    const running = searches.filter((s) => s.status === "running").length;
    if (running && !confirm(`现在有 ${running} 个查找在进行，重启后会自动继续。确定重启吗？`)) return;
    await api("POST /api/update_install");
    closing = true;
  } finally {
    el.disabled = closing;
  }
}

Object.assign(ACTIONS, {
  "update-check": checkUpdate,
  "update-install": installUpdate,
  "update-later": async () => { seeUpdate(await api("POST /api/update_later")); },
  "update-reveal": () => api("POST /api/update_reveal"),
  "open-logs": () => api("POST /api/open_logs"),
});

/* pywebview announces its bridge with "pywebviewready"; an empty api object may exist before that */
if (typeof window.pywebview?.api?.call === "function") pollUpdate();
else addEventListener("pywebviewready", pollUpdate, { once: true });
