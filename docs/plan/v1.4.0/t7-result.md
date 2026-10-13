# v1.4.0 — T7: the packaged app updates itself (2026-10-13, macOS 26 on Apple silicon)

Builds made with PyInstaller from this branch in a clean environment (Python 3.13, the project installed non-editable): 1.4.0 = the app under test, 1.4.1 = the "newer release" (same code, version bumped, zipped like the CI does: `ditto -c -k` of a folder holding `找岗位助手.app` and `安装.command`, 74.4 MB). A local server stood in for GitHub (`DJS_UPDATE_API`): the release JSON with `size` and `digest`, the download with Range support. Every run had its own `HOME` and data folder; nothing touched the real ones.

## Smoke test of the 1.4.0 build (`packaging/smoke.sh`, same as CI)
selftest of both programs; `djs call provider_list` 14 platforms; `djs call status` reports version 1.4.0, packaged, update none (the R9 version check); one real crawl (牛客 20 jobs); MCP over stdio through `djs` and through `找岗位助手 mcp`, 19 tools each; the `.app` opens and keeps running 8 s with nothing on stderr. 193 MB unpacked.
It failed once before that: the app wrote its log lines to stderr, because `mcp` puts a stderr handler on the root logger and our lines travelled up to it (and httpx's request line with GitHub's URL). Fixed in `djs/log.py` (`propagate = False`, httpx at WARNING) with a test.

## First start in an empty home (acceptance)
The app wrote our entry into all twelve assistants' files without asking, creating the folders of the ones that were not there (`~/.codebuddy`, `~/.qwen`, `~/.kimi-code` too), and the ten read-only allow rules into `~/.claude/settings.json`; the settings page shows twelve tiles, all on.

## Update flow
| Case | Result |
|---|---|
| 1.4.0 started against the server | found 1.4.1 about 5 s after start, downloaded, size and sha256 checked, unzipped, the new build's own `--selftest` passed: ready 2.4 s later. 「更新已就绪」 with 「安装并重启」 / 「下次启动时安装」 on screen (captured from the app window only) |
| 「安装并重启」 | window closed, swap in 2 s, new version started from the same path: `status` says 1.4.1, `app.frozen` true; the old build is in `<data>/updates/previous/`; `last.json` ok; the assistants' entries still point at the same program; `codesign --verify --deep --strict` passes (ad-hoc), no quarantine mark |
| 「下次启动时安装」, then closing the window | the message closed, `install_on_quit` saved; on quit the swap happened and the new version started again by itself |
| Same two flows with the app in `/Applications` (started from a terminal, and started with `open` so the system treats the app itself as the actor) | swap done, no system prompt, no refusal, no "damaged" or "unidentified developer" dialog; test app removed from `/Applications` afterwards |
| Wrong sha256 on the release | refused (warning in the log), nothing offered, nothing left staged |
| The Python-side log | the lines are in `<data>/logs/djs.log`, no query string of the download URL |

## Not covered here
Intel Mac and Windows (the CI helper test and smoke cover the helper itself; nobody has run either by hand); an app installed by an administrator into a folder the user cannot write (reported by `can_install`, not tried); the 「已更新到 1.4.1」 toast after a swap (covered by a browser test, not looked at on the real window); the first real update from a published release (needs the release after this one).
Behaviour to know: quitting with 「下次启动时安装」 pending swaps the build and starts the new one again at once, because the helper always relaunches (Pencil's updater, built on Squirrel.Mac, is understood to do the same; not verified).
