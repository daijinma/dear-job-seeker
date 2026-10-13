"use strict";
/* Shared state, the bridge to Python and small helpers. The scripts of this page share one scope;
   ui_app.SCRIPTS gives their order. Anything that comes from outside (job text, errors, typed input) must go
   through esc() before it is put into markup. */

let POLL_MS = 3000;         // how often a running search is read again
const S = {                 // what survives a page change
  providers: [], logins: [], prefs: {}, picked: null, form: { keyword: "", city: "" }, open: {}, proxyOn: false,
  searched: false,          // the 查找 page knows of at least one search: the site picker shrinks to a line
};
const ACTIONS = {};         // data-act name -> handler(element, event); every script adds its own
let epoch = 0;              // bumped on each navigation: async work started on an older page must not paint
let pollTimer = 0;
let toastTimer = 0;

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const siteLabel = (name) => S.providers.find((p) => p.name === name)?.label || name;
const loginOf = (name) => S.logins.find((x) => x.provider === name);
// a login worth asking for: one that measurably helps and is missing, or one the user had that has expired
const wantsLogin = (x) => (x.helps !== false && !x.logged_in) || x.state === "expired";
/* what logging in gives, to follow "登录后：" (a text that already starts with 登录后 is not made to say it twice) */
const gives = (unlocks) => (unlocks || "结果更全").replace(/^登录后[，,：:]?/, "");

/* how long ago a unix time was, in the words the pages use: "刚刚", "5 分钟前", "3 天前" */
function since(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "刚刚";
  const [n, unit] = s < 3600 ? [s / 60, "分钟"] : s < 86400 ? [s / 3600, "小时"] : [s / 86400, "天"];
  return `${Math.floor(n)} ${unit}前`;
}

/* innerHTML only when the markup changed, so a poll that renders the same content keeps focus and hover */
function paint(el, html) {
  if (el._html !== html) { el.innerHTML = html; el._html = html; }
}

function banner(text) {
  const el = $("#banner");
  el.textContent = text;
  el.hidden = !text;
}

/* a message that fades by itself (after `ms`, else 3.6 s); with `action` ({ label, name, data }) it carries a button and
   stays longer */
function toast(text, tone = "", action = null, ms = 0) {
  const el = $("#toast");
  el.textContent = text;
  if (action) el.insertAdjacentHTML("beforeend", btn(action.label, action.name, action.data, "line sm"));
  el.className = `toast show ${tone}${action ? " act" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), ms || (action ? 10000 : 3600));
}

/* Python answers {error} for what went wrong on its side (shown as text); a throw means the bridge itself failed. */
async function api(route, payload = {}) {
  let data;
  try {
    data = await window.pywebview.api.call(route, payload);
  } catch (e) {
    banner("和程序的连接断开了，请关闭窗口后重新打开。");
    throw Object.assign(new Error("和程序的连接断开了"), { bridge: true });
  }
  banner("");
  if (data.error) throw new Error(data.error);
  return data;
}

/* the one place where an unhandled failure becomes visible */
const fail = (e) => { if (!e.bridge) toast(e.message || String(e), "bad"); };

/* Run fn every POLL_MS while it returns true. A newer pollWhile or any navigation replaces the loop. */
function pollWhile(fn) {
  clearTimeout(pollTimer);
  const e = epoch;
  pollTimer = setTimeout(async () => {
    if (e !== epoch) return;
    try { if (await fn(e)) pollWhile(fn); } catch (err) { fail(err); }
  }, POLL_MS);
}

/* Modal and drawer share one mechanism: an overlay that is filled, shown and focused, and gives focus back to
   whatever opened it when it closes. Escape and a click on the dim area press the layer's [data-close] button.
   While a layer is open the page behind it is inert: no Tab and no screen reader gets there. */
const LAYERS = ["modal", "drawer"];
const layerOpen = () => LAYERS.some((id) => !$("#" + id).hidden);
const freezeBackground = (on) => document.querySelectorAll(".top, #banner, #view").forEach((el) => { el.inert = on; });

function showLayer(id, html) {
  const el = $("#" + id);
  if (el.hidden) el._opener = document.activeElement;
  paint(el, html);
  el.hidden = false;
  freezeBackground(true);
  (el.querySelector("[data-autofocus]") || el.querySelector("button, a[href]"))?.focus();
}

function hideLayer(id) {
  const el = $("#" + id);
  if (el.hidden) return;
  el.hidden = true;
  el._html = "";
  freezeBackground(layerOpen());
  el._opener?.focus?.();
}

ACTIONS.scrim = (el, e) => { if (e.target === el) el.querySelector("[data-close]")?.click(); };
