# v1.4.0 design

## What we take from Pencil (Pen.app 1.2.16: `app-update.yml`, `out/updater.js`, `out/menu.js`, `out/app.js`, `@ha/mcp`, the renderer's settings and toasts) and what we do not
Taken as it is:
| Pencil | Here |
|---|---|
| `electron-updater`, GitHub provider on a public releases repository; check at start and every 30 min; `autoDownload` on, `autoInstallOnAppQuit = false`; silent when offline (`net.isOnline`, `NET::ERR` ignored) | R1, R2: same cadence, same behaviour, our own small client (no Electron) |
| Toast "Update Available — Downloading new version…" (3 s, only for a manual check); persistent toast "Update Ready — A new version has been downloaded and is ready to install." with **Install and Restart** / **Install on next launch**; `installOnAppQuit` remembered, reset after use, no toast when already set | R3: the same two messages and two buttons in Chinese |
| App menu item "Check for Updates…" that becomes "Restart & Install Update" once downloaded; dialog "No Updates Available" | R3: a settings row with one button that changes the same way; no native menu (pywebview's menu is fixed when the app starts and its labels cannot change) |
| `installUpdateIfPending` at quit → `quitAndInstall` | `update.apply_on_exit()` after `webview.start()` returns; the helper does the swap (R4) |
| Integrations: a fixed list of supported assistants shown as a grid (4 columns, 2 when narrow) of icon + name + switch; caption "pen.dev automatically sets up MCP in enabled tools when it starts."; below, "Connect another MCP client" with a copy-config button | R5: same layout and caption in Chinese; our twelve assistants; the four without a config file go into the "other client" block |
| `enabledIntegrations` unset on first start → **all supported enabled, written silently**; no detection of installed tools; folders and files created when missing | R5: same |
| At every start: remove, then write, for the enabled ones; a switch applies at once (`toggle-mcp-integration`) | R5: the enabled ones are written again at every start (an entry already right is not rewritten); a switch writes or removes at once |
| Entry = `{command, args}` (+ `type: "stdio"` for Claude Code / Claude Desktop; OpenCode `mcp` key with `type: "local"`, `command: [..]`; Codex TOML `mcp_servers`); existing keys of our own entry are kept (`{...existing, ...new}`) | kept from 1.2.6 (the same shapes) |
| Claude Code: adds `mcp__pencil` to `~/.claude/settings.json` `permissions.allow`, removes it with the integration | R6, narrowed to the ten read-only tools |
| Log file (`electron-log`, debug) | R8 |

Not taken, with the reason:
| Pencil | Here |
|---|---|
| Developer-ID signing and notarization; a signature check by the updater | unsigned (A10, A23): sha256 from the release API + the staged build's selftest stand in |
| Crash reports to a vendor service | none: offline tool in a public repository (A27) |
| Agent skills shipped inside the app (`skills/pen-dev`) | none: the server's `INSTRUCTIONS` carry the flow and have tests |
| `--app` / `--agent` arguments of its MCP binary | none: nothing is sent anywhere |
| Plain `writeFileSync` of the whole config, no backup, an unparsable file throws | kept from 1.2.6, because it cannot be seen in the UI and loses nothing: one backup `.djs-bak`, atomic replace, an unreadable file refused and shown |
| Cleans every supported integration at each start | only entries of switches turned off *in the app* are removed, at the moment of the click: an entry the user pasted by hand into an assistant whose switch was never on stays |

## Updater (`djs/update.py`, about 250 lines, httpx and stdlib only)
State: `none → downloading → ready → (installing)`, plus `failed(note)`; kept in memory. `ui_prefs.json` holds `update = {install_on_quit}`.

```
check()      GET https://api.github.com/repos/daijinma/dear-job-seeker/releases/latest   (If-None-Match with the last ETag; env DJS_UPDATE_API overrides, tests and T7)
             tag "vX.Y.Z" > app_version() (numeric compare, not string); asset name == dear-job-seeker-X.Y.Z-<target>.zip
download()   starts at once after a check that found one: <data>/updates/<ver>/<name>.part, Range resume, then sha256 + size == asset.digest/size, rename
stage()      unzip → <data>/updates/<ver>/stage ; run `<stage>/…/djs --selftest` (30 s) ; must print "selftest ok" → state ready
```
- `<target>` comes from the running build: frozen + `sys.platform` + `platform.machine()` of this process.
- Never runs in a source checkout (`app_version()` is `None` or not frozen); `DJS_UPDATE=0` switches it off for a locked-down machine.
- One daemon thread started after the window is shown, one check at a time (a lock), a 30-minute loop; no work on the UI thread. The window learns the state by polling `GET /api/update` every few seconds while open (the UI has no push channel).
- Proxy: plain `httpx` with the system's proxy settings (the user's Clash stays on); the release download redirects to GitHub's asset host, same path.
- Text shown to the user comes from the state only (Chinese); the log gets the English detail.

Routes (ui_api, same JSON shape as the connect routes): `GET /api/update` → `{current, latest, state, note, install_on_quit}`, `POST /api/update_check` (manual: returns what was found), `POST /api/update_later` (sets `install_on_quit`), `POST /api/update_install` (quit now and install). `status` and `/api/about` read the same state.

## Connect (R5, R6), changes against 1.2.6's `djs/connect.py`
- `TARGETS` stay twelve; `detected` no longer gates anything: `connect(key)` creates missing folders and the file (the 1.2.6 rule "only when detected" goes). Backup, atomic write, unreadable-file refusal, own-entry-only, idempotence, the changed-while-writing retry stay.
- Prefs: `connect = {"enabled": [keys]}`; the key absent means all twelve. Migration from 1.2.6 (`{on, asked}`): `asked` true or `on` not empty → `enabled = on`; otherwise absent.
- `sync(keys)` at app start for the enabled ones: writes what is off or stale, in a background thread (the window never waits), errors go to the log and show as the assistant's state.
- Routes: `GET /api/connect` → `{command, config, enabled, targets: [{key, label, state, path}], guided: [{key, label, where}]}`, `POST /api/connect {key, on}` → same shape. `connect_many`, `connect_asked` are removed.
- Claude Code allow rules: `ALLOWED_TOOLS` constant, read-modify-write of `~/.claude/settings.json` with the same safety (a file that cannot be read is left alone and only logged), added with the switch and at sync, removed with the switch.
- UI: `connect.js` renders the grid (a coloured round tile with the assistant's first letter stands in for a logo: no brand artwork is bundled), the caption, the divider and 「连接其他 MCP 客户端」; the first-launch card and `connectAsk` go from `search.js`.

## Install (helper, written to `<data>/updates/` at install time)
macOS (`apply.sh`, run with `/bin/sh`, detached with `start_new_session`):
1. wait until the app's pid is gone (up to 60 s);
2. `mv "<app>" "<data>/updates/previous/"`, `ditto "<stage>/找岗位助手.app" "<app>"`; on any failure move the old one back;
3. `xattr -dr com.apple.quarantine` on the new one (a no-op for our own download, harmless);
4. `open "<app>"`; write `<data>/updates/last.json` `{version, ok, note}` for the next start to show.
The bundle path is `…/找岗位助手.app` found from `sys.executable`; if it is not writable (installed by an administrator) or lives in `/AppTranslocation/` (A25's rule), the message says so and offers 「打开下载好的位置」 instead.

Windows (`apply.ps1`, started with `powershell -NoProfile -WindowStyle Hidden`): wait for `找岗位助手.exe`; retry up to 60 s while any file of the folder is held (an assistant's `djs.exe`: name the process in `last.json`); move the folder to `previous`, copy the staged folder in, restart `找岗位助手.exe`. Files are UTF-8 with BOM, CRLF (lessons of A23).

Both helpers take every path as an argument (quotes, spaces, Chinese), print nothing to the user, and are covered by `packaging/update-check.sh` on all three runners (T2).

Running assistants keep the old code until they restart their MCP server; the settings row says 「已连接的 AI 助手要重启一次才会用新版」.

## MCP additions
- `status.update` (R7); `status.app = {version, frozen, path}` so a mismatch like today's stale `1.2.5` is visible.
- `login_cancel(provider)` → `service.login_cancel`; docstring says it stops a window opened by `login`.
- `INSTRUCTIONS` gets one line: if `status.update.state` is `ready`, tell the user an update waits in the app.

## Log (R8, `djs/log.py`, stdlib `logging` + `RotatingFileHandler`)
One logger `djs`; `logging.getLogger("djs.update")` etc. A filter strips `?query` parts and values of keys named like cookie/token/key/secret. The window's 「打开日志文件夹」 uses the opener behind the existing 「打开数据文件夹」 (`ui_api._show`).

## Release contract (R9)
A step at the end of the `release` job:
`gh release view "$TAG" --json assets` → exactly three names matching `dear-job-seeker-<ver>-(macos-arm64|macos-x86_64|windows-x64).zip`, each with a non-empty `digest`; `<ver>` equals the tag and `pyproject.toml`. The same pattern string is imported by `djs/update.py` tests so the two cannot drift.

## Limit tests (T6): how each is run
Rules for every live run: the user says go; one platform per session; the first sign of refusal (HTTP 403/429, a challenge page, code 37-like business codes, empty where the previous page had data) ends that platform's run and starts its cooldown; results go to `docs/plan/v1.4.0/limits-result.md` as "this combination at this time", never as a permanent fact; no BOSS or 猎聘 unless named; login platforms use a copy of the profile, deleted afterwards.

| # | Test | Local part (CI) | Live part |
|---|---|---|---|
| L1 | pace floor | fake site that answers 429 below N s: the pacer backs off and the cooldown starts | for each public platform: 5 searches at the default pace (control), then at `min_interval`, then at half of it; record the first refusal and what it looks like |
| L2 | budget per hour | — | keep one public platform at default pace for 60 min of back-to-back searches; count requests until the first refusal; one platform per day |
| L3 | detail-hop budget | fake site that refuses after N detail calls: the task finishes the list, marks the rest incomplete, cools down once | 新华英才, 58同城, 前程无忧: one search with details to the end, counting calls and time |
| L4 | page caps | — | for each platform with `page_cap`: ask page cap and cap + 1; the cap holds when the last page has data and the next is empty or refused |
| L5 | session dies mid-crawl | fake login platform whose probe flips after the list: the task ends `waiting_login`, no anonymous request follows (extends A24's tests to every `login` platform) | 康强 with a copy of its profile whose cookie is deleted after the list call |
| L6 | challenge page | fake site returning a slider/captcha page with 200: outcome is `failed` with a stated reason, never `no_results` | 国聘, 中智, 牛客: look at what they return when asked too fast (part of L1) |
| L7 | exits | two fake exits, one dead: the pool rests it, the other serves | per platform, one search direct and one through the user's proxy: outcome, time, and the exit's address country. Needs the user's go: it is the answer to the network question |
| L8 | long run | a 2 h soak with fake sites in CI is too long: 10 min with a scheduled search every minute | 8 h unattended with a search per hour on public platforms; memory, threads, files, cookie lifetime |
| L9 | Windows | `update-check.sh` and the smoke test on the runner | none until a Windows machine is available |

## Test design notes
- T4 uses `mcp.shared.memory.create_connected_server_and_client_session`, so the real protocol is exercised in-process and fake providers still work (a subprocess could not use them).
- T1/T2 never touch the network: `DJS_UPDATE_API` points at a local `http.server` for the helper tests and T7.
- T5 and the L tests live in `tests/live/`, skip themselves at import (`pytest.skip(allow_module_level=True)`) unless `DJS_LIVE=1`, so the default run and CI never reach a site.
