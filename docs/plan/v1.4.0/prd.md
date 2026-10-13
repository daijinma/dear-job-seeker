# v1.4.0 — the app updates itself, assistants connect the Pencil way, limits get tests

Baseline: v1.2.0 (`docs/plan/v1.2.0/`, A1–A27, F1–F57) and v1.3.0 (Playwright removal; independent of this plan, built after it). Only the change is written here. Starting point and evidence: `check-1.2.6.md`. **Update and assistant connection follow Pencil (Pen.app 1.2.16, read locally) as exactly as this app allows** (user, 2026-10-13, A28); the mapping and the deliberate differences are in `design.md`.

## Why
- An installed app never learns a new build exists: 1.2.0 → 1.2.6 took three days and every one needed a manual download, unzip and helper run.
- A build can only update itself if it already contains the updater, so the first such build must still be installed by hand: the earlier it ships, the more later builds reach people.
- 「连接 AI 助手」 (1.2.6) asks first, detects installed assistants and splits them into groups; Pencil lists every supported assistant with one switch, turns all on at first start and keeps the entries current at every start. The user wants that behaviour and look.
- An assistant cannot cancel a login window it opened, cannot tell the user the app is old, and nothing tests the tools through the protocol.
- Crawl limits are known from notes and a few live runs; nine are untested (`check-1.2.6.md` §3) and nothing repeats them.

## Requirements
| # | Requirement |
|---|---|
| R1 | **Check** (Pencil: at start and every 30 min, silent when offline). At start (after the window shows, never delaying it) and every 30 min while open, plus a manual check. Source: this repository's GitHub release marked latest (drafts and pre-releases never count), one conditional request per check (ETag). An offline or failing check is silent and logged; a 403/429 backs off until the reset time. A source checkout never checks. |
| R2 | **Download** (Pencil: `electron-updater` downloads by itself, `autoInstallOnAppQuit = false`). A found update downloads at once in the background, only the asset of this build's kind (`macos-arm64`, `macos-x86_64`, `windows-x64`, from the running build). Resumable; size and sha256 must equal the release API's `size` and `digest` (no extra asset: the three-asset rule stays). Unzipped to `<data>/updates/<version>/`; the staged build must pass its own `--selftest` before it counts as downloaded. |
| R3 | **Prompts, word for word in Pencil's structure.** Manual check finding one: short message 「有新版本」「正在下载新版本…」(3 s); nothing found: message 「当前没有可用的更新」. Download finished: a persistent message that cannot be dismissed, title 「更新已就绪」, text 「新版本已下载好，可以安装了。」, two buttons 「安装并重启」 and 「下次启动时安装」 (the second remembers `installOnAppQuit`, closes the message and installs when the app next quits, then resets the flag); not shown again when `installOnAppQuit` is already set. A settings row 「软件更新」 shows the version and one button: 「检查更新」, or 「重启并安装更新」 once an update is downloaded (Pencil's menu item changes the same way). No skip, no switch, no top-bar pill. |
| R4 | **Install.** Quit-and-install: a helper started detached after the app has exited swaps the build, keeps the old one in `<data>/updates/previous/` until the next update and starts the new one. A failed swap restores the old build; the next start says what happened. Locked files (an assistant's running `djs`) are waited for up to 60 s, then the message names what to close. Running searches resume after the restart (tasks are handed back on a normal stop, F37). |
| R5 | **Connection the Pencil way** (replaces 1.2.6's ask-first card). The settings card 「连接 AI 助手」 is a grid of every supported assistant (no detection, no groups, no "not found" list): icon, name, one switch; the line 「找岗位助手启动时会自动在已开启的 AI 助手里配置 MCP。」; below a divider 「连接其他 MCP 客户端」 with a 「复制配置」 button and where to paste it (Qoder, Trae, 通义灵码, Cherry Studio, others). First start with nothing saved: **all supported assistants are switched on and written silently**, no question. At every start the enabled ones are written again (the app may have moved); a switch applies at once. Entry name `dear-job-seeker`. The one-time ask card on 查找 and its routes are removed. |
| R6 | **Claude Code needs no click per call** (Pencil adds `mcp__pencil` to `~/.claude/settings.json` `permissions.allow`, and removes it when the integration goes). Same here, narrowed: allow rules `mcp__dear-job-seeker__<tool>` for the ten read-only tools (status, provider_list, search, wait_for_search, jobs_get, job_detail, search_list, task_list, login_status, login_check); `login`, `logout`, `crawl_start`, `set_*`, `egress_*` keep asking. Removed with the switch. |
| R7 | **MCP.** `status.update` = `{current, latest, state: none \| downloading \| ready \| failed, note}`; new tool `login_cancel(provider)`; installing is not an MCP action. |
| R8 | **Log.** `<data>/logs/djs.log`, rotating (1 MB × 3), UTF-8, no cookies, tokens or keys (URLs without query strings); written by the updater, the connection sync, login windows, start-up tasks and crawl failures; 「打开日志文件夹」 next to the version row. |
| R9 | **Release contract.** After the three zips are uploaded, the release job fails unless: exactly the three assets `dear-job-seeker-<version>-<target>.zip` exist, each has a `digest`, and the tag equals the version in `pyproject.toml` and in the built app. |
| R10 | **Test features** T1–T7 below, in the repository and run by CI where they need no live site. |

## Test features
| ID | What | Where it runs |
|---|---|---|
| T1 | `tests/test_update.py`: fake release API (`httpx.MockTransport`): newer / same / older / pre-release / draft; ETag 304; asset choice per build; wrong digest or size refused; interrupted download resumes; offline silent; 403/429 back-off; a source checkout never checks; a staged build whose selftest fails is not downloaded; `installOnAppQuit` set, used and reset | pytest, CI |
| T2 | Install swap on a fake build tree: paths with spaces and Chinese, old build kept, failed swap restores it, locked file waits then reports, second update replaces `previous`; `packaging/update-check.sh` runs the real helper on the three CI runners against a staged old build and a local release server and requires: swapped, relaunched, old kept | pytest + CI (3 OS) |
| T3 | UI: the ready message and its two buttons, no message when `installOnAppQuit` is set, the 3 s messages, the settings row in both states; the assistant grid (all supported listed, switches, first-start state all on, copy button, error toast); the 查找 page no longer shows an ask card | `tests/test_ui_browser.py` style |
| T4 | MCP contract: every tool called through the protocol in-process (`create_connected_server_and_client_session`) with fake providers: argument validation, JSON result, error shape, `status.update`, `login_cancel` with a fake login window; the real-stdio test also calls `status` and `provider_list` | pytest, CI |
| T5 | Live MCP acceptance script `tests/live/mcp_acceptance.py` (skipped unless `DJS_LIVE=1`): the sequence run on 2026-10-13 (status, search, wait, jobs_get, job_detail, login_status, login_check, login window open/cancel on a fresh data folder); prints a report; BOSS only with `--boss` | by hand |
| T6 | Limit tests L1–L9 (`design.md`), local ones with fake sites in CI (L5, L6 fake side, concurrency), live ones opt-in, one platform per session, stop at the first refusal, never BOSS or 猎聘 unless named | by hand, with the user's go-ahead |
| T7 | After build: a real update end to end on the packaged app against a local release server (no real release needed); on the first real later release, the 1.4.x app updates itself and the result is written into that release's decisions | by hand |

## Not in this version
Code signing (A10); a beta channel; delta updates; crash reporting or any telemetry (Pencil sends crashes to a vendor service; this app stays offline); a skip-this-version choice or an update switch (Pencil has none); Linux; new platforms; assistants beyond the twelve of 1.2.6 (Pencil's Windsurf and Copilot are not added).

## Acceptance
- Full suite green; CI: three builds, smoke, `update-check.sh` on three OS, the R9 check passing on the release job.
- A packaged 1.4.0 pointed at a local release server with a newer build: it finds it, downloads, verifies, shows 「更新已就绪」; after 「安装并重启」 the new version runs from the same path and `status.version` says so; with a wrong digest nothing is offered (T7 by hand on macOS arm64 here; Intel and Windows through the CI helper test only: nobody runs Windows by hand).
- A fresh data folder (home pointed at an empty folder): first start writes our entry into all twelve assistants' files without asking, the settings grid shows twelve switches on, one switch off removes only our entry; Claude Code's `settings.json` carries the ten allow rules and loses them with the switch.
- `status.update` and `login_cancel` present; the contract test passes for all tools.
- `check-1.2.6.md` §3: each of L1–L9 either run with its result recorded, or recorded as not run with the reason.

## Risks
| Risk | Answer |
|---|---|
| macOS refuses an app swapping its own bundle (App Management, Gatekeeper on an unsigned build) | tried first on the packaged build in T7 (and E2 below); our own download carries no quarantine mark, so no first-run prompt is expected, but that is assumed until tried |
| Windows locks files a running `djs.exe` of an assistant holds | wait and name; the CI runner holds a file in the test; not hand-tested on a real Windows machine |
| A 70 MB download over the user's proxy is slow or cut | resume, silent retry at the next check; 1.3.0's smaller build halves it |
| GitHub rate limit (60 requests/h per address without a token) | one conditional request per 30 min (2 requests an hour), back-off on 403/429 |
| A bad release reaches every installed app | selftest of the staged build, old build kept, R9 check; no forced restart |
| Writing into every assistant's folder, installed or not (Pencil does) | only our own entry; backup once, atomic write, an unreadable file is refused and shown (kept from 1.2.6); the switch removes the entry |
| Auto-allowing tools in Claude Code | the ten read-only tools only (R6); the rest keep asking |
| Limit tests cost the user's IP or accounts | opt-in, one platform per session, hours apart, stop at the first refusal, public platforms first; login platforms only after the user agrees |
