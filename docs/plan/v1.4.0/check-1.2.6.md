# v1.4.0 — check of 1.2.6 (2026-10-13)

What the user asked: do login, MCP connection, status, search, results and the login call work through MCP; does the app update itself; which crawl limits were tested. Run on the 1.2.6 code, a real stdio MCP session (`python -m djs.server`, the same entry the assistants start), the real data folder, BOSS 直聘 left out on purpose (A24: code 37 yesterday).

## 1. MCP, called as an assistant would
| Call | Result |
|---|---|
| `status` | ok, no network: workers, queue (12 done, 0 stuck), exits (`direct`), browser (Chrome found), logins (智联, BOSS, 前程无忧, 猎聘, 康强 all `logged_in`), per-platform configured / missing keys / last error. Shows `version 1.2.5` on this source checkout: the editable install's metadata is stale (dev only; a download reads its own bundle). |
| `provider_list` | ok: 14 platforms with auth, pace, page caps, transport |
| `search` (13 sites, 测试工程师 · 成都, 1 page) → `wait_for_search` | ok: status `done`, 249 records, 209 once merged; 11 sites found jobs, 公共招聘网 and 康强医疗网 `no_results`, 0 failed, nothing cooled down |
| `jobs_get` (`title=测试`, `floor_month_salary=10000`, `limit=3`) | ok: 50 matches, 3 returned with `next_offset`, salary converted to month figures, short summaries |
| `job_detail` | ok: full description and welfare tags |
| `login_status`, `login_check` (智联 only) | ok; `login_check` is one request |
| `login` on a platform already logged in | returns `logged_in` at once, session timestamp refreshed |
| `login` on a fresh data folder (no session) | window started (browser processes on the new profile); the client went away after 30 s and the window was closed with it (0 processes left). Page content and finishing a real login were not looked at in this round (earlier rounds: F43). |
| `logout`, `set_*`, `egress_*`, `crawl_start` | not called: they change the user's real state |

Gaps found
| # | Gap |
|---|---|
| G1 | No `login_cancel` tool although `service.login_cancel` exists: a host that gives up waits for the 180 s timeout or must drop the connection |
| G2 | `status` knows nothing about the app's own version being outdated (there is no updater) |
| G3 | The only MCP test over real stdio lists the tool names; no test calls tools through the protocol (argument validation, JSON results, errors) |
| G4 | No log file in the data folder: a failure of a start-up task, a login window or (later) an update leaves nothing to read |

## 2. Self-update
None. `grep` finds no release lookup, download or replace code; the README tells users to download by hand. A new build is only found by the user visiting the Releases page. The release API already carries what an updater needs: three assets with `size` and `digest: sha256:…` each (checked on v1.2.6), so no fourth asset is needed (three-asset rule).

## 3. Crawl limits: tested vs not
Tested (evidence in `docs/plan/v1.2.0/decisions.md`)
| Area | Where |
|---|---|
| Pace per platform and what happens below it (BOSS 8 s floor 6 s, 猎聘 4 s) | A2, A8 |
| BOSS: blanked pages under automation, risk answers, anonymous vs login; code 37 after ≈5 details | F14–F16, F36, F44, F56, A21, A24, E1 (`docs/plan/v1.3.0/e1-result.md`) |
| Result caps from the platforms (智联 60, 国聘 400, 中智 400, 前程无忧 1000, 猎聘 80, 赶集 8 per page anonymous) | provider notes, F6, F38 |
| Logged in vs anonymous per platform | F38, F45 |
| Cooldown, retry, dead proxy, no network | F37, F47, F48, F52–F54 |
| City and keyword handling | F2, F34, F46 |
| Two processes on one database; start while workers commit | F25, F37 |
| 30-minute soak (6 searches, 72 tasks): memory, threads, files | F55 |
| 30 000-job database speed | F51 |
| One search on 13 sites at default pace, today | above: 0 failures |

Not tested
| # | Limit | Why it matters |
|---|---|---|
| L1 | Where each platform starts refusing when the pace is below default (only BOSS has a measured floor) | defaults are a guess elsewhere; too slow wastes minutes, too fast costs the user's IP |
| L2 | Budget per hour / per day per platform (requests, details) | repeated searches by an assistant add up |
| L3 | Detail-hop budget: 前程无忧 145 details ≈ 8 min was run once; 新华英才, 猎聘, 58同城 never beyond one search | the details are most of the traffic |
| L4 | Do the page caps still hold (a site moves them without notice) | a wrong cap hides jobs or asks pages that fail |
| L5 | A session that dies in the middle of a crawl, per login platform | A24 promises "never anonymously"; only BOSS and 康强 are covered by tests, none live |
| L6 | Captcha / slider pages on public platforms: is it named, not read as "no jobs" | F12 covers empty results; a challenge page is a different answer |
| L7 | Behaviour through different exits: the user's proxy (Clash, always on) vs direct, per platform; overseas exits against domestic sites | the user reports network trouble; no per-site evidence yet. The controller of Clash could not be reached from this session, so the policy per site is unknown |
| L8 | Unattended runs longer than 30 min (a day, scheduled searches) | memory, cookies, cooldown recovery over hours |
| L9 | Windows at runtime (WebView2, antivirus, paths with Chinese names) | CI builds and smoke-tests it, nobody has used it |
