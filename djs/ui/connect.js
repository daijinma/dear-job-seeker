/* 连接 AI 助手: the card on 设置, a grid of every supported assistant with one switch each. Every /api/connect route
   answers in one shape: {command, config, enabled, targets: [{key, label, state, path}], guided: [{key, label,
   where}]}. */

const CONNECT_HINT = { stale: "需要更新", unreadable: "配置文件读不懂，请用下面的复制配置" };   // only what is not as it should be

/* a round badge with the assistant's first letter, in a colour that follows its key (no brand artwork is bundled) */
const connectBadge = (t) => {
  const hue = [...t.key].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);
  return `<span class="avatar" style="background:hsl(${hue} 50% 40%)" aria-hidden="true">${esc(t.label[0])}</span>`;
};

function connectTile(t) {
  const on = t.state === "connected" || t.state === "stale";
  return itemRow({
    name: t.label, hint: CONNECT_HINT[t.state], lead: connectBadge(t),
    end: switchBtn(on, `连接 ${t.label}`, act("connect-toggle", { k: t.key, s: t.state })),
  });
}

function connectCard(data) {
  const guided = data.guided.map((g) => `<p class="sub"><b>${esc(g.label)}</b> · ${esc(g.where)}</p>`);
  return `<div class="card connect" id="connect-card" data-config="${esc(data.config)}"><h2>连接 AI 助手</h2>
    <p class="sub">让你电脑上的 AI 助手直接用本程序查岗位，不用自己改配置文件。</p>
    <p class="sub caption">找岗位助手启动时会自动在已开启的 AI 助手里配置 MCP。</p>
    <div class="tiles">${data.targets.map(connectTile).join("")}</div>
    <section class="block"><div class="row"><h3>连接其他 MCP 客户端</h3>${btn("复制配置", "connect-copy")}</div>
      <div class="guided">${guided.join("")}</div></section></div>`;
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

/* A switch: off to on writes our entry, on to off removes it, and one that is on but out of date (it points to where the
   app was) is written again; the card shows what the route answered. A refusal leaves the card as it was. */
async function connectSet(el) {
  const e = epoch;
  const { k: key, s: state } = el.dataset;
  const on = state !== "connected";
  el.disabled = true;
  try {
    const data = await api("POST /api/connect", { key, on });
    if (e !== epoch) return;
    $("#connect-card").outerHTML = connectCard(data);
    $(`#connect-card [data-act="connect-toggle"][data-k="${key}"]`)?.focus();   // the repaint replaced the button the keyboard was on
    const label = data.targets.find((t) => t.key === key).label;
    toast(on ? `${state === "stale" ? "已更新" : "已连接"} ${label}，重启它后生效` : `已断开 ${label}`, "ok");
  } catch (err) {
    if (err.bridge) throw err;
    toast(`没能更新这个 AI 助手的配置：${err.message}`, "bad");
  } finally {
    el.disabled = false;
  }
}

Object.assign(ACTIONS, {
  "connect-toggle": connectSet,
  "connect-copy": async (el) => {
    const ok = await copyText(el.closest("[data-config]").dataset.config);
    toast(ok ? "已复制" : "没能复制到剪贴板，请再试一次", ok ? "ok" : "bad");
  },
});
