/* Login dialog, one for every site that can use a login (the user scans a QR code in a real browser window).
   States: waiting (spinner + countdown) -> ok (check, closes itself) | failed (reason + retry). */

const LOGIN_SECONDS = 180;
const SUCCESS_MS = 1200;
let L = null;   // the login in progress: { provider, label, state: waiting|ok|failed, deadline, error, kind }

const mmss = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

const LOGIN_BODY = {
  waiting: (run) => `${spinner()}<p class="lead">请在刚弹出的浏览器窗口里扫码登录</p>
    <ol class="steps"><li><i>1</i>在弹出的窗口里找到二维码</li><li><i>2</i>打开手机上的${esc(run.label)} App，扫一扫并确认登录</li><li><i>3</i>登录成功后，这里会自动提示你</li></ol>
    <p class="count">还剩 <b id="count">${mmss(LOGIN_SECONDS)}</b></p>
    <p class="safe">${icon("shield", 18)}登录只在你自己的电脑上完成，账号信息不会上传到任何地方。</p>`,
  ok: () => `${icon("check", 36)}<p class="lead">登录成功</p><p class="sub">等待中的查找会自动继续。</p>`,
  failed: (run) => `${icon("alert", 36)}<p class="lead">登录没有成功</p><p>${esc(run.error)}</p>${
    run.kind === "browser" ? '<p class="sub">请先在终端运行 <code>playwright install chromium</code>，再重新打开本程序。</p>' : ""}`,
};

function loginFoot(state) {
  const close = (text) => btn(text, "close-modal", { close: "" }, "");   // [data-close]: what Escape and the dim area press
  if (state === "waiting") return close("取消");
  return state === "failed" ? close("关闭") + btn("重试", "login-retry", {}, "primary") : "";
}

function paintLogin() {
  showLayer("modal", `<div class="card modal" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
    <h2 id="dlg-title">登录 ${esc(L.label)}</h2>
    <div class="state ${L.state}">${LOGIN_BODY[L.state](L)}</div>
    <div class="actions">${loginFoot(L.state)}</div></div>`);
}

function closeLogin() {
  L = null;
  hideLayer("modal");
}

async function startLogin(provider) {
  L = { provider, label: siteLabel(provider), state: "waiting", deadline: Date.now() + LOGIN_SECONDS * 1000 };
  const run = L;
  paintLogin();
  try {
    await api("POST /api/login", { provider });
  } catch (e) {
    closeLogin();
    throw e;
  }
  loginTick(run).catch(fail);
}

function finishLogin(run) {
  run.state = "ok";
  paintLogin();
  toast(`已登录 ${run.label}`, "ok");
  route().catch(fail);   // the search page and the settings pick up the new login at once
  setTimeout(() => { if (run === L) closeLogin(); }, SUCCESS_MS);
}

async function loginTick(run) {
  if (run !== L) return;   // cancelled or replaced
  const { login } = await api("GET /api/login_status", { provider: run.provider });
  if (run !== L) return;
  if (login.state === "ok") return finishLogin(run);
  if (login.state === "failed") {
    Object.assign(run, { state: "failed", error: login.error, kind: login.kind });
    return paintLogin();
  }
  const left = Math.round((run.deadline - Date.now()) / 1000);
  $("#count").textContent = left > 0 ? mmss(left) : "正在确认…";
  setTimeout(() => loginTick(run).catch(fail), 1000);
}

Object.assign(ACTIONS, {
  login: (el) => startLogin(el.dataset.k),
  "login-retry": () => startLogin(L.provider),
  "close-modal": closeLogin,
});
