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

## Client API (fixed 2026-10-16, so the pieces can be built side by side)
`djs/browser.py` after the change (everything else of the module stays as 1.4.0 has it: `_Cdp`, `_Browser`, `Window`, `plain_window`, `profile_lock`, `profiles_in_use`, `on_domain`, `find_browser`, `executable`, `_clear_leftovers`, closing rules):
- Gone: `_pw`, `launch_context`, `open_page`, `open_context`, `_attach`, `_guard`, `_chromium`, the `None` (Playwright Chromium) channel, `install_chromium`, `INSTALL_HINT`, `proxy_settings`, `STEALTH_JS`, `ready()` that asked for Playwright.
- `ready() -> bool`: `cdp_url() is not None or find_browser() is not None`. `no_browser_hint()`: one sentence, install Google Chrome or Microsoft Edge (no download option, no command).
- `attached_cookies() -> list[dict]`: every cookie of the browser `DJS_CDP_URL` names (`Storage.getCookies` at browser level); replaces `open_context(...).cookies()` in `auth.py`.
- `open_browser(prov, exit_id, interval, jitter)` (context manager, plain threads or not: nothing here needs a thread without an event loop) → `BrowserClient`. Our browser: the platform profile under `profile_lock`, `_clear_leftovers`, `--remote-debugging-port=0`, `WINDOW_FLAGS`, `--headless=new` unless `_headless(prov)` is false, `--proxy-server=<host:port>` for an exit that is a proxy without a login (a proxy URL with a user name is refused with `BrowserUnavailable`, A31); closed by `_Browser.close()` however the block ends. With `DJS_CDP_URL`: the user's browser, only our tabs are touched and closed.
- `BrowserClient` (paced like an httpx client; the same `request/get/post(url, params, headers, data, json, content, timeout, paced)` returning `httpx.Response`, built from an in-page `fetch` by `Runtime.evaluate` with `awaitPromise` + `returnByValue`, no `Runtime.enable`):
  - `goto(url, timeout=30)`: paced; closes the current tab, `Target.createTarget("about:blank")`, `Target.attachToTarget(flatten)`, `Page.navigate` (no `Page.enable`), then the state machine of this file (poll `location.href` / `document.readyState` under one deadline; `errorText`, a blank page or an `about:` / `chrome-error:` URL end as `httpx.ConnectError`, never as an empty success; where a page landed after its redirects (another host, a verification page) is for the provider to judge: BOSS looks for its security check, the others fail on their next step, a cookie that never appears or a fetch that cannot be made).
  - `wait_cookie(name, timeout=10)`: polls `document.cookie`; `httpx.ReadTimeout` on timeout. `eval(js, arg=None)`: the JS function, called with `arg`, result by value. `html() -> str`. `url -> str`.
  - `cookies(url=None) -> list[dict]` (browser level, of that URL's host family), `cookie_value(name, url=None) -> str | None`, `clear_cookie(name)` (browser level, the expiry trick of `Window.delete_cookies`), `settle(ms)` (document complete and the URL unchanged for 1 s, at most `ms`; never raises).
  - `request` on a tab whose execution context died during the call → `httpx.ConnectError` (the engine retries those; BOSS maps it to Blocked); in-page fetch error text `timeout` → `httpx.ReadTimeout`.
  - `sniff` is dropped (nothing uses it).
- Providers: `client.page...` goes away. `providers/_browser.py` `fresh_visit` = first visit of a client: `clear_cookie(token)`; every visit: `before = cookie_value(token)`, `goto`, `settle`, wait until the value is set and differs from `before` (10 s, polled); not changed → `Blocked("...token not renewed")`. `zhaopin` reads `client.cookies(HOME)`. `status` (service) loses the `playwright` key of `browser`.
