# dear-job-seeker — PRD v1.2.0 (usable, not just demo-able)

Baseline: [`../v1.1.0/prd.md`](../v1.1.0/prd.md). Only changes are listed.

## Why
A real run of the desktop app (driven through Peekaboo) and of the MCP server (driven by an end-to-end script over stdio) showed the v1.1.0 build looks right but is not yet usable:

| Observed | Evidence |
|---|---|
| One serial worker: 9 sites take 10+ min, later sites never start | 160 s in, 4 of 9 tasks done; zhaopin still fetching details at 3.5 s/job |
| Detail hop per job dominates latency | 20 jobs × 3 s per page |
| Repeating a search gives an empty result page | `jobs.task_id` only records the *first* discovering task |
| City is not honoured everywhere, silently | chinahr returned 北京/深圳 rows for "杭州"; iguopin returned 0 rows (needs GB/T codes); nowcoder city column empty (wrong key) |
| Boss over plain HTTP = `code 37` | reproduced earlier; see below |
| GUI app has no shell env | keys for yupao/job51/job58 were invisible when launched from Finder |

## Ground truth about Boss (measured 2026-10-11, own machine)
| Fetch method | Result |
|---|---|
| plain HTTP (httpx), valid cookie | `code 37` environment abnormal (internal notes) |
| Playwright Chromium, **headless** | redirected to home, `code 37` |
| Playwright Chromium, **headed** (+ AutomationControlled off) | `code 0`, 331 results |
| Real browser (Ego Lite), page `fetch` of `joblist.json`, **not logged in** | `code 0`, 15 jobs with `securityId`, `encryptJobId`, company, city, district, experience, degree, skills |
| Same, salary field | **empty when not logged in** (`salaryDesc: ""`; DOM salary span is empty too) |

Consequences: Boss needs a *real browser transport* (headed); anonymous browsing already works; **login only unlocks salary** → Boss becomes `auth = "optional"`, not "login required".

## Decisions
| # | Decision | Why |
|---|---|---|
| D11 | One daemon worker thread **per platform** (`claim(provider=…)`) | pacing is already per (platform, exit); platforms must not queue behind each other |
| D12 | `crawl_start(detail=…)`; UI search is **list-only** by default, detail fetched **on demand** (`job_detail`) | latency; most cards need no detail |
| D13 | `task_jobs(task_id, job_id)` link table; results are per search (keyword+city) | repeat searches must show results |
| D14 | `djs/cities.py` single city table; provider attr `city_filter = server \| client \| none`; engine post-filters `client` platforms by row city | honest, uniform city semantics |
| D15 | **Capture transports**: `http` (httpx, default) · `browser` (Playwright persistent profile, headed/headless) · `cdp` (attach to the user's own Chromium via `DJS_CDP_URL`). Provider declares `transports`, user may override (`set_transport`). The browser client is a drop-in for `httpx.Client` (in-page `fetch` returns an `httpx.Response`) plus `goto / html / eval / sniff` | providers keep one parsing path; Boss is only possible this way |
| D16 | `auth` values: `public` · `optional` (works anonymously, login adds data) · `login` (required) | Boss |
| D17 | `.env` autoload from `$DJS_HOME` and cwd (never overrides real env) | GUI apps have no shell env |
| D18 | `djs call <tool> [json]` and `djs work [--until-idle]` CLI | scriptable, testable by agents and cron |
| D19 | Ego-browser is used for *research and verification* here, not shipped as a dependency; CDP attach covers real-browser use | avoid a proprietary hard dependency |

## Page plan (non-technical users)
1. **查找** – keyword + city + default sites (common, no keys); "更多网站" disclosure for Boss / key-needed / city-required sites; recent searches with per-site progress.
2. **我的结果** – pick a search, filter by site, cards, load more, detail drawer (lazy detail), export CSV; real empty/loading/error states.
3. **设置** – speed presets, accounts (Boss: "登录后可看薪资"), advanced proxy; no jargon.

## Verification plan
- Unit/contract tests (no network) stay green.
- `scripts`-free live audit per platform: field fill-rates + city-match rate (kept in the final report).
- Real MCP host: `claude -p` with the project MCP config drives `provider_list → crawl_start → task_list → jobs_get`.
- Real window: Peekaboo drives the native window page by page; screenshots reviewed.
- Boss: headed browser transport verified live anonymously; QR login verified with the user present.

## Boss response shapes (verified live in a real browser, anonymous, 2026-10-11)
- List `GET /wapi/zpgeek/search/joblist.json?scene=1&query=<kw>&city=<boss city code>&page=<n>&pageSize=15` → `code 0`, `zpData.{resCount, hasMore, lid, jobList[]}`. Item keys used: `securityId`, `encryptJobId`, `lid`, `jobName`, `salaryDesc` (empty unless logged in), `brandName`, `cityName`, `areaDistrict`, `businessDistrict`, `jobExperience`, `jobDegree`, `jobLabels[]`, `skills[]`, `brandIndustry`, `brandScaleName`, `welfareList[]`, `bossName`, `bossTitle`.
- Detail `GET /wapi/zpgeek/job/detail.json?securityId=<id>&lid=<lid>` → `code 0`, `zpData.jobInfo.{postDescription, address, salaryDesc, experienceName, degreeName, locationName}`, `zpData.brandComInfo`, `zpData.bossInfo`.
- Salary is hidden for anonymous users in both list and DOM; the job detail page URL is `/job_detail/<encryptJobId>.html`.
- Page prerequisites: the page JS writes `__zp_stoken__`; the request must run inside a page on `www.zhipin.com` (headed browser or real browser), with header `x-requested-with: XMLHttpRequest`.
