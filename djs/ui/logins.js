/* 平台登录: every site that has a login, whether it is logged in, what logging in gives, and the buttons to change that. */

const LOGIN_STATE = { logged_in: ["已登录", "ok"], expired: ["已过期", "warn"], not_logged_in: ["未登录", "warn"] };
/* how the user logs in (Login.method), in the words of the page */
const LOGIN_HOW = {
  qr: (x) => `用 ${x.label} App 扫码`, wechat: () => "用微信扫码", password: () => "输入账号密码", sms: () => "短信验证码",
};

function loginCard(x) {
  const [text, tone] = LOGIN_STATE[x.state];
  const how = LOGIN_HOW[x.method]?.(x);
  const facts = [how && `登录方式：${how}`, x.saved_at && `上次登录：${since(x.saved_at)}`].filter(Boolean).map(esc).join(" · ");
  const buttons = x.logged_in
    ? btn("重新检测", "login-check", { k: x.provider }) + btn("退出登录", "logout", { k: x.provider })
    : btn("登录", "login", { k: x.provider }, "primary sm");
  return `<article class="card login-card"><div class="row"><h2>${esc(x.label)}</h2>${pill(text, tone)}</div>
    ${x.unlocks ? `<p class="unlock">登录后：${esc(gives(x.unlocks))}</p>` : ""}
    ${facts ? `<p class="sub small">${facts}</p>` : ""}
    <div class="actions">${buttons}</div></article>`;
}

function viewLogins() {
  const body = S.logins.length
    ? `<div class="grid2">${S.logins.map(loginCard).join("")}</div>`
    : emptyState("现在没有需要登录的网站", "所有网站都可以直接查找，不用登录。");
  $("#view").innerHTML = `<section class="narrow logins"><h1>平台登录</h1>
    <p class="sub">登录后，有些网站能给你更完整的岗位信息。登录只在你自己的电脑上完成，账号信息不会上传到任何地方。</p>${body}</section>`;
}

Object.assign(ACTIONS, {
  "login-check": async (el) => {
    el.disabled = true;
    try {
      const x = await api("POST /api/login_check", { provider: el.dataset.k });
      toast(x.logged_in ? "登录状态正常" : "登录已经失效了，请重新登录", x.logged_in ? "ok" : "bad");
    } finally {
      el.disabled = false;
    }
    await route();
  },
  logout: async (el) => {
    await api("POST /api/logout", { provider: el.dataset.k });
    toast("已退出登录");
    await route();
  },
});
