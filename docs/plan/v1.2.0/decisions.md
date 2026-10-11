# v1.2.0 — decisions and real-use findings

Decisions taken without a round-trip are marked `assumed:` and can be reversed.

## Decisions
| # | Decision | Why |
|---|---|---|
| A1 | assumed: `jobs_get` and the results page show complete jobs by default; incomplete ones stay one switch away with their reason | D21 says complete data is the final data |
| A2 | assumed: BOSS pacing 8 s (floor 6 s) | the 拓岗宝 team measured 8–20 s as the safe range; 6 s was below it |
| A3 | assumed: 猎聘 filters by city on the client | its API takes no city parameter |
| A4 | assumed: browser transport prefers an installed Chrome, then Edge, then the bundled Chromium | no 150 MB download for most users |
| A5 | assumed: CSV export writes to the Downloads folder instead of a save dialog | the native save dialog appeared as a blank modal window and froze the app (see F3) |
| A6 | assumed: 字节跳动招聘 stays in the list although it never publishes salaries; its jobs count as incomplete and the UI explains why | removing a working site would hide real jobs; the explanation keeps the contract honest |
| A7 | assumed: release v1.2.0 is tagged only after logins are verified with real accounts | logins are the remaining unverified surface |
| A8 | assumed: 猎聘 paced at 4 s (not 12 s) | at 12 s the ~80 detail hops of one search take ~20 min; 4 s was clean in live runs |
| A9 | assumed: platforms usable without any user action (public or optional login, keys present, no city needed, no browser window) are preselected; BOSS (opens a window) and login-required sites live under "更多网站" | the default search must not silently lose 智联 / 前程无忧 after they gained an optional login |
| A10 | assumed: the downloads are not code-signed; the README explains the one-time "Open Anyway" / "Run anyway" step | signing needs a paid Apple Developer ID and a Windows certificate |
| A11 | assumed: one download per OS holds both the window and the console `djs` (the MCP command) on one shared runtime | one ~75 MB zip instead of two; the window and MCP use the same data folder |
| A12 | assumed: the Intel Mac build runs on the native `macos-15-intel` runner; a repository variable switches it to Rosetta on the Apple-silicon runner | GitHub retires Intel runners over time |
| A13 | assumed: the CI smoke test includes one real crawl (nowcoder); a release whose build cannot crawl fails instead of shipping | an untested download is worse than a late one |
| A14 | assumed: duplicates are merged only across platforms; one platform's postings with the same title stay separate | in a live run all three same-site repeats were distinct postings (other pay, other JD) |
| A15 | assumed: `search` without `providers` uses the same "usable right now" set as the app's preselection (A9), minus platforms that have no code for the asked city; BOSS only when named | an AI client must not open a browser window or wait for a login nobody asked for |
| A16 | assumed: one `wait_for_search` call blocks at most 600 s and then answers `timed_out: true` | MCP clients time out long calls; calling again is cheap |
| A17 | assumed: `jobs_get` lists newest first; the results page keeps its interleaved order | a client filters and pages; the page is read top to bottom |
| A18 | assumed: the app also preselects the sites that need a city (58同城, 赶集) and uses them only when a city is filled in; a site that cannot take a search is skipped and named instead of refusing the whole search (amends A9) | a typed city should reach every site that covers it, and one uncovered city must not cost the other sites |
| A19 | assumed: the login window is a plain browser window (the user's Chrome / Edge started as a normal program), read only at the browser level; crawling stays on Playwright | BOSS and 猎聘 blank any page Playwright drives (F36) |
| A20 | assumed (user: 先完成初版再上传): 1.2.0 ships with Playwright; 1.3.0 replaces it with the browser-level CDP client the login window already uses | Playwright's Node.js driver is 65–70% of every download (Windows 107 of 166 MB, macOS 136–139 of 195–201 MB unpacked); without it ≈ 60 MB unpacked, ≈ 25–30 MB zipped |

## Findings from driving the real app (Peekaboo on the native window, fresh data folder)
| # | Finding | Status |
|---|---|---|
| F1 | 9-site search "Python 开发 · 杭州": all sites done in ~20 s, 411 jobs | ok |
| F2 | NowCoder returned 0 rows for any keyword with a space ("Python 开发"), 4 without it | fixed (keyword words joined) |
| F3 | 导出表格 opened the save dialog as a blank, detached, modal window; the whole app stopped responding | fixed (export to Downloads) |
| F4 | After 开始查找 the progress card sat below the fold and the top bar scrolled away | fixed (card under the search box, picker collapses, sticky header) |
| F5 | Default order "最新发现" bunched one site on top (all jobs found in the same minute) | fixed (interleave sites; add 薪资从高到低) |
| F6 | 公共招聘网 "会计 · 杭州" = 0 | not a bug: the site has few local postings ("会计 · 北京" = 17) |
| F7 | MCP server: a platform's worker thread could die at startup and leave its tasks pending forever | fixed (DB init once per process, worker loops never exit) |

## Findings from a real AI-client session (a job seeker's request over MCP: "杭州 Python 后端，月薪 ≥1.5 万，挑 5 个")
The client produced 5 sound recommendations, and reported these tool problems:
| # | Finding | Status |
|---|---|---|
| F8 | `jobs_get` returns every full JD; 40–300 jobs overflow the client's context | fixed (`fields='summary'` by default: 20 jobs 16.5 KB instead of 41.2 KB; `limit`/`next_offset`) |
| F9 | No server-side filters (minimum salary, text must contain, city) | fixed (`min_month_salary`, `max_month_salary`, `text`, `city`, `provider`, all in SQL) |
| F10 | Salary units mixed (元/时, 元/天, 元/月, 元/年) cannot be compared | fixed (`salary_month_min` / `salary_month_max` on every job) |
| F11 | The same job appears on several sites (智联 + 前程无忧) | fixed (fingerprint; one card per job with `also_on`, see A14) |
| F12 | "0 jobs" does not say whether the site had none or blocked us | fixed (`outcome` per site in `search_list`, `task_list` and `wait_for_search`) |
| F13 | The client fetched while details were still being completed (51job 3/96) and incomplete jobs were hidden | fixed (`completing {done, total}`; `wait_for_search`; "正在补全详情" instead of a wrong reason) |

## Findings from live BOSS runs (real browser transport, anonymous)
| # | Finding | Status |
|---|---|---|
| F14 | A passed security check leaves `?_security_check=...` in the URL; treating any such URL as blocked put BOSS into a 30-min cooldown on every run | fixed (only the verification page path counts) |
| F15 | BOSS blanks an automated page a few seconds after it loads; a request sent after the 8 s pacing wait always failed ("Failed to fetch", "Failed to parse URL") | fixed (pace → load page → call API at once; one wait per visit) |
| F16 | Clearing the anti-bot token before every load made every load bounce through the security check and kill the next call | fixed (token cleared once per session; one retry after a late redirect) |
| F17 | Anonymous BOSS jobs can never be complete (no salary), yet the completion pass fetched a detail for each (8–14 s apiece) | fixed (`Provider.detail_can_fill`; BOSS: only when logged in or the salary is there) |
| F18 | With 智联 / 前程无忧 now "optional login", the search page moved them out of the default selection | fixed (grouping rule A9) |
| F19 | GUI automation stops while the Mac is locked; flows were continued headless against the real backend (same page, real DB, workers and network) | note |

## Findings from building the downloads
| # | Finding | Status |
|---|---|---|
| F20 | PyInstaller's BUNDLE made the console `djs` the app's main program: double-clicking would have started an MCP server that exits at once | fixed (explicit `CFBundleExecutable`, not background-only) |
| F21 | `fcntl` (pacing and profile locks) does not exist on Windows: the whole app failed to import there | fixed (`djs/filelock.py`: flock / msvcrt) |
| F22 | Printing Chinese crashes a Windows build that uses the ANSI code page | fixed (both programs run in UTF-8 mode) |
| F23 | The packaged MCP server printed a traceback on exit (stdout flushed after the client closed it) | fixed |
| F24 | `cryptography` ≥ 49 ships no macOS Intel wheels | Intel build pins `cryptography<49` |

## Findings from the MCP tool work
| # | Finding | Status |
|---|---|---|
| F25 | Starting a search next to running workers failed about 1 run in 6 with "database is locked" (the app's search start had the same flaw) | fixed (the write lock is taken first; regression test) |
| F26 | Live run "Python 后端 · 杭州": 12 sites in 2 min 34 s, 9 found jobs, 3 had none, 0 failed; 147 records, 107 complete; one job on 智联 and 前程无忧 merged; ≥ 1.5 万/月 leaves 74 | ok |

## Findings from the second round of real use (desktop tour and an AI client, same day)
| # | Finding | Status |
|---|---|---|
| F27 | AI client, same request as F8–F13: it used search → wait_for_search → jobs_get with filters and picked 5 sound jobs in 5 turns, no context trouble | ok |
| F28 | 鱼泡 jobs had no link, so the client could not recommend them (e.g. 阿里云 2–4 万/月) | fixed (`www.yupao.com/zhaogong/<jobId>.html`, checked on a live job) |
| F29 | `text="Python"` also matched descriptions, so Java and 数据分析 roles came back | fixed (`title` filter; live: `title=会计` 125 vs `text=会计` 147) |
| F30 | `wait_for_search(timeout=300)` timed out while 前程无忧 fetched its last details (no JD in its list: 145 details at 3 s ≈ 8 min); the client read the results anyway | note: the lists are there at once and the details fill in |
| F31 | 猎聘 lists some companies anonymously ("某知名互联网公司") | site behaviour |
| F32 | Desktop tour "Python 后端 · 杭州": every flow worked, no page errors, but one search showed 215 / 216 / 213 jobs (search card, strip, heading) | fixed (`whole`: one count everywhere) |
| F33 | 综合 put "风电结构仿真分析工程师" (国聘) first for "Python 后端" | fixed (title matches first in 综合) |
| F34 | One site that cannot take the city refused the whole search; "浙江杭州" and "杭州市西湖区" were refused by every site, 义乌 and 昆山 by nearly all | fixed (city text resolved; a site that cannot take it is skipped and named; live: 义乌 started 牛客, skipped 智联 and 58同城) |
| F35 | 58同城 and 赶集 were not preselected even with a city typed | fixed (A18) |
| F36 | Real logins: 智联, 前程无忧 and 康强 worked; BOSS and 猎聘 showed the login page for ~1 s, then about:blank. Any Playwright-driven page is blanked within 1–2 s there (4/4 runs, with or without stealth scripts); a plain Chrome read only at the browser level kept both pages alive (4/4) | fix in progress (A19) |
| F37 | A process that ended in the middle of a 前程无忧 detail pass left its task 'running'; other processes could not take it for 15 minutes (the lock is set once at claim time and never refreshed) | fix planned: heartbeat with every progress write, 3-minute lock, tasks handed back on a normal stop |
| F38 | Logged-in vs anonymous, same searches (copies of the user's real logins): 康强 15 complete vs none (login required: works); 智联 20 vs 20 complete on page 1 (no gain seen; its anonymous API already carries the JD); 前程无忧 0 vs 50 complete: logged in it moved to the PC search in a browser, which hung on the WAF cookie (20 s timeout) until the site cooled down | 前程无忧 fixed (ac076ac: a login no longer switches the transport); 智联 over 3 pages (Java · 杭州): logged in 60 (59 complete) in a browser, anonymous 60 (60 complete) in 7 s, only 21 shared: a login changes which jobs come back, not how many; kept as is, union of both left to the user |
| F39 | AI client, "宁波 会计，月薪 8000 以上，最好双休" on 1b7d88b: 12 sites done, 222 complete jobs, 5 sound picks with links (one alternative from 58同城) in 12 turns | ok |
| F40 | `text="双休"` missed the best job: its 双休 was only in 前程无忧's welfare tags, which were not stored | planned (W13: `benefits`) |
| F41 | `min_month_salary` is an overlap, so 6k–9k came back for "8000 以上" | planned (W13: `floor_month_salary`) |
| F42 | `job_detail` returned the platform's raw JSON (share links, HR data): noisy for a client | planned (W13: clean by default, `raw=True`) |
| F43 | Real BOSS + 猎聘 logins through the plain window: both saved, both pass the site's own check; the crawl browser reads the session the window stored | ok |
| F44 | BOSS logged in: the list came back with salaries (15/15), but every detail call failed ("Failed to fetch": the page was blanked before the call), the task retried 8 times and a later list call failed too | mitigated (a call the page could not make → Blocked, 30-min cooldown); fix planned for 1.3.0: crawl BOSS in a plain browser too (A20). Live BOSS tests stopped to protect the account |
| F45 | 猎聘 logged in vs anonymous, Java · 杭州, 2 pages: 6 vs 6, all shared (its API needs no login; 74 of 80 dropped by the city filter) | login line reworded: no measurable gain |
