# v1.4.0 implementation order

Tests first within each step (red → green). Branch: `feat/self-update`, merged after step 7's check. Release numbers follow build order (next free 1.x.y); the plan version only orders plans. v1.4.0 is built before v1.3.0 (user, 2026-10-13).

| Step | What | Check |
|---|---|---|
| 1a | **Connect, the Pencil way** (R5, R6): `djs/connect.py` (no detection gate, `enabled` prefs + migration, allow rules, first start writes all), routes, `djs/ui_app.py` sync in a thread, `connect.js` grid + "other client" block, `settings.js`, remove the ask card from `search.js`; T3 for this part | existing connect tests updated, new cases (first start all on, migration, allow rules added/removed, unreadable settings.json left alone); UI cases; a fresh `HOME` run: twelve entries written, one switch off removes one |
| 1b | `djs/log.py` and its wiring (R8); `djs/update.py`: check, download, verify, stage, install-on-quit flag (R1, R2); T1 (own files only: runs beside 1a) | pytest T1; one real request against this repository's v1.2.6 release with the app at an older fake version; a digest flipped by one byte is refused |
| 2 | Helpers `packaging/apply.sh`, `apply.ps1` and the swap in `djs/update.py` (R4); T2 including `packaging/update-check.sh` | local pytest; `update-check.sh` on this Mac |
| 3 | Update routes, polling, the 「更新已就绪」 message, the settings row, exit-time install (R3); T3 for this part | `tests/test_ui_browser.py` cases; the desktop app run from source against a local release server: found → downloading → ready, 「安装并重启」 and 「下次启动时安装」 both reach the helper (helper stubbed: it only writes its arguments) |
| 4 | MCP: `status.update`, `status.app`, `login_cancel`, the `INSTRUCTIONS` line (R7); T4 | contract test over all tools; the real-stdio test calls `status`; tool list in `test_mcp_stdio.py` updated |
| 5 | CI: R9 step in `release.yml`, `update-check.sh` on the three runners; README (Download → "updates itself", `DJS_UPDATE=0`, log location, the new connection behaviour); decisions | `actionlint` clean; the R9 step run by hand against v1.2.6 (must pass) and against a release with a missing asset (must fail) |
| 6 | Full suite, review of the whole diff (`trellis-check`); packaged build of this Mac | suite green; build opens, selftest ok |
| 7 | **T7 on the packaged app**: a local release server holds a newer fake build → found, downloaded, verified, 「更新已就绪」, 「安装并重启」, the new version running from the same path, assistants' entries still valid; wrong digest → nothing offered; the macOS self-swap question (R4, Risks) answered here, also from `/Applications`; a fresh `HOME`: first start writes the twelve entries | recorded in `docs/plan/v1.4.0/t7-result.md`; if the swap is refused anywhere, R4's text changes and the card offers 「打开下载好的位置」 there |
| 8 | T5 script written and run once on the real data folder (BOSS excluded), report saved; L-tests: local parts in CI, live parts only with the user's go, in this order: L7 (the network question), L4, L6, L1, L3, L5, L2, L8 | `limits-result.md`: each L either with its measured result or "not run, reason" |
| 9 | Release with three builds (the first build that can update itself; the notes say it must be installed by hand once). The next release is the real end-to-end check of the updater | assets present, R9 step green; the following release: 1.4.x → newer, written into its decisions |

## Order against v1.3.0
They touch different files (v1.3.0: browser layer, providers, spec; this plan: updater, ui, status, CI helper step). 1.4.0 first, because only builds that contain the updater can update themselves, and 1.3.0's halving of the download then makes every later update cheaper; 1.3.0 then ships as an update of 1.4.x, so its zip-size gate and spec change must keep `update-check.sh` green.

## Not decided here
- Whether the live limit tests (step 8) run at all, and when: the user's call, platform by platform.
