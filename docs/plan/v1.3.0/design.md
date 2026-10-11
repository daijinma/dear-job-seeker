# v1.3.0 design — plain-browser crawling

## Shape
- `djs/browser.py` keeps one browser layer. What 1.2.0 built for the login window (`find_browser`, `_Cdp`, `_Browser`, `_clear_leftovers`, profile lock, exit handler) becomes the base of crawling too; Playwright's `launch_context` / `open_browser` / `BrowserClient` are replaced.
- A crawl session = our browser process on the platform profile (or the user's browser with `DJS_CDP_URL`) + one tab per visit, created with `Target.createTarget` and driven through a flattened page session (`Target.attachToTarget`, `flatten: true`).
- Page work without the domains detection scripts watch: `Page.navigate` (no `Page.enable`), in-page `fetch` by `Runtime.evaluate` with `awaitPromise` and `returnByValue` (no `Runtime.enable`, no binding, no init script). `--disable-blink-features=AutomationControlled` keeps `navigator.webdriver` false, so the stealth init script goes.
- Navigation is a small state machine under one deadline: `Page.navigate` (its `errorText` fails the visit) → poll `location.href` and `document.readyState` until the URL is one the provider allows and the document is complete → the provider's own readiness check (e.g. a fresh `__zp_stoken__`). A blank page, a verification page, a URL outside the allowed set, or an execution context destroyed during a call end the visit as an error (Blocked where the provider says so); none of them can yield an empty success.
- A visit is a critical section per platform: navigate → ready → request → close the tab, one at a time; the tab is not a token boundary (cookies are the profile's). The first visit of a session still clears the stale token, as `fresh_visit` does (clearing before every load made every load bounce through the security check: F16). Later visits: the token's value is read before `Page.navigate`; ready = the value differs afterwards; if it has not changed within the visit's deadline the visit fails as Blocked (no request on a token of unknown age). E1 checks that BOSS writes a new value on every load; if it rewrites the same value, this rule is replaced by a page-side readiness signal found in E1.
- `_Cdp` routes replies by session id; it keeps dropping events while no flow needs them. An event buffer is added only if E1 selects passive capture.
- Cookies stay browser-level (`Storage.getCookies` / `Storage.setCookies`), as in the login window.
- Headless crawling uses `--headless=new` with the same flags; BOSS (`needs_headed`) gets a window.

## Interface kept
Providers and the engine see the same client: `get` / `post` (paced unless told otherwise) → `httpx.Response`, `goto`, `eval`, `wait_cookie`; the `page` uses in `providers/_browser.py` are rewritten against the new client, with the token rule above.

## BOSS, after E1
Default plan if E1 shows evaluate-only pages survive: per call, new tab → navigate to the search page → fresh `__zp_stoken__` → in-page fetch → close tab. If they do not: a separate provider flow, designed only once E1 shows how a specific job's detail is triggered and matched (the page's own list response by query and page, a detail by job id); without that, BOSS takes the tier prd R3 names (list-only only if its list is proven on its own, else off).

## Removed
`playwright` dependency, `collect_data_files("playwright")` in the spec, `djs install-browser` (Chromium download), Playwright paths in `selftest` and the smoke test, STEALTH_JS, Playwright fakes in tests (the fake DevTools server of 1.2.0 takes their place).

## Size check
Expected unpacked: Windows ≈ 60 MB, macOS ≈ 60–65 MB (Python runtime, cryptography via mcp, pydantic, the two programs); zipped ≈ 25–30 MB.
