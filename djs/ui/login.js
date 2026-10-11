/* Login dialog, one for every site that has a login (the user logs in inside a real browser window that closes itself).
   States: waiting (spinner + countdown) -> ok (check, closes itself) | failed (reason + retry). Cancel closes the window. */

const LOGIN_SECONDS = 180;
const SUCCESS_MS = 1200;
let L = null;   // the login in progress: { provider, label, method, state: waiting|ok|failed, deadline, error, kind }

const mmss = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

/* what the waiting dialog asks of the user, by how the site logs in (Login.method) */
const LOGIN_STEPS = {
  qr: (run) => ["在弹出的窗口里找到二维码", `打开手机上的${run.label} App，扫一扫并确认登录`],
  wechat: () => ["在弹出的窗口里找到二维码", "打开手机上的微信，扫一扫并确认登录"],
  password: () => ["在弹出的窗口里找到登录框", "输入你的账号和密码，完成登录"],
  sms: () => ["在弹出的窗口里输入手机号", "收到验证码后填进去，完成登录"],
};
const LOGIN_DONE_STEP = "登录成功后，窗口会自动关闭，这里也会提示你";
const FAILED_HINT = {
  browser: "请先安装 Google Chrome 或 Microsoft Edge 浏览器，再重新登录。",
  busy: "这个网站正在查找，等它结束后再点“重试”。",
};

const loginSteps = (run) => [...(LOGIN_STEPS[run.method] || LOGIN_STEPS.password)(run), LOGIN_DONE_STEP]
  .map((text, i) => `<li><i>${i + 1}</i>${esc(text)}</li>`).join("");

const LOGIN_BODY = {
  waiting: (run) => `${spinner()}<p class="lead">请在刚弹出的浏览器窗口里登录</p>
    <ol class="steps">${loginSteps(run)}</ol>
    <p class="count">还剩 <b id="count">${mmss(LOGIN_SECONDS)}</b></p>
    <p class="safe">${icon("shield", 18)}登录只在你自己的电脑上完成，账号信息不会上传到任何地方。</p>`,
  ok: () => `${icon("check", 36)}<p class="lead">登录成功</p><p class="sub">等待中的查找会自动继续。</p>`,
  failed: (run) => `${icon("alert", 36)}<p class="lead">登录没有成功</p><p>${esc(run.error)}</p>${
    FAILED_HINT[run.kind] ? `<p class="sub">${esc(FAILED_HINT[run.kind])}</p>` : ""}`,
};

function loginFoot(state) {
  const close = (text, name) => btn(text, name, { close: "" }, "");   // [data-close]: what Escape and the dim area press
  if (state === "waiting") return close("取消", "login-cancel");
  return state === "failed" ? close("关闭", "close-modal") + btn("重试", "login-retry", {}, "primary") : "";
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

/* cancel really stops the login: the window on the user's screen closes, not only this dialog */
async function cancelLogin() {
  const { provider } = L;
  closeLogin();
  await api("POST /api/login_cancel", { provider });
}

async function startLogin(provider) {
  L = { provider, label: siteLabel(provider), method: loginOf(provider)?.method, state: "waiting", deadline: Date.now() + LOGIN_SECONDS * 1000 };
  const run = L;
  paintLogin();
  try {
    await api("POST /api/login", { provider });
  } catch (e) {
    closeLogin();
    throw e;
  }
  if (run !== L) return api("POST /api/login_cancel", { provider });   // cancelled while it was still starting
  loginTick(run).catch(fail);
}

function finishLogin(run) {
  run.state = "ok";
  paintLogin();
  toast(`已登录 ${run.label}`, "ok");
  route().catch(fail);   // the badge, the search page and the results pick up the new login at once
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
  "login-cancel": cancelLogin,
  "close-modal": closeLogin,
});
