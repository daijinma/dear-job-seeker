/* Navigation and the page-wide event wiring. Runs last. */

const VIEWS = { search: viewSearch, results: viewResults, settings: viewSettings, logins: viewLogins };

async function route() {
  clearTimeout(pollTimer);
  const e = ++epoch;
  hideLayer("drawer");
  const [, tab = "search", query = ""] = location.hash.match(/^#\/(\w+)(?:\?(.*))?$/) || [];
  document.querySelectorAll("nav a").forEach((a) => (a.dataset.tab === tab ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  const { providers, logins, ...prefs } = await api("GET /api/providers");
  if (e !== epoch) return;
  Object.assign(S, { providers, logins, prefs });
  paintBadge();
  await (VIEWS[tab] || viewSearch)(new URLSearchParams(query), e);
}

/* the number of sites that have a login but are not logged in (or whose login ran out), next to 平台登录 */
function paintBadge() {
  const todo = S.logins.filter(wantsLogin).length;
  const badge = $("#login-badge");
  badge.textContent = todo;
  badge.hidden = !todo;
  const link = $('nav a[data-tab="logins"]');
  todo ? link.setAttribute("aria-label", `平台登录，还有 ${todo} 个建议登录`) : link.removeAttribute("aria-label");
}

function viewError(e) {
  if (e.bridge) return;
  $("#view").innerHTML = emptyState("暂时打不开这一页", e.message, btn("重试", "reload", {}, "primary"));
}

const run = (fn, el, event) => (async () => fn(el, event))().catch(fail);

ACTIONS.reload = () => route().catch(viewError);

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (el && ACTIONS[el.dataset.act]) run(ACTIONS[el.dataset.act], el, e);
});
document.addEventListener("submit", (e) => {
  e.preventDefault();
  if (ACTIONS[e.target.dataset.submit]) run(ACTIONS[e.target.dataset.submit], e.target, e);
});
document.addEventListener("input", (e) => {
  if (e.target.name in S.form) S.form[e.target.name] = e.target.value;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("#modal:not([hidden]) [data-close], #drawer:not([hidden]) [data-close]")?.click();
});
addEventListener("hashchange", () => route().catch(viewError));
addEventListener("unhandledrejection", (e) => fail(e.reason));

/* pywebview announces its bridge with "pywebviewready"; an empty api object may exist before that */
if (typeof window.pywebview?.api?.call === "function") ACTIONS.reload();
else addEventListener("pywebviewready", ACTIONS.reload, { once: true });
