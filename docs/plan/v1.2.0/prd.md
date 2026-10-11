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
- Real MCP host (headless) with the project MCP config drives `provider_list → crawl_start → task_list → jobs_get`.
- Real window: Peekaboo drives the native window page by page; screenshots reviewed.
- Boss: headed browser transport verified live anonymously; QR login verified with the user present.

## Boss response shapes (verified live in a real browser, anonymous, 2026-10-11)
- List `GET /wapi/zpgeek/search/joblist.json?scene=1&query=<kw>&city=<boss city code>&page=<n>&pageSize=15` → `code 0`, `zpData.{resCount, hasMore, lid, jobList[]}`. Item keys used: `securityId`, `encryptJobId`, `lid`, `jobName`, `salaryDesc` (empty unless logged in), `brandName`, `cityName`, `areaDistrict`, `businessDistrict`, `jobExperience`, `jobDegree`, `jobLabels[]`, `skills[]`, `brandIndustry`, `brandScaleName`, `welfareList[]`, `bossName`, `bossTitle`.
- Detail `GET /wapi/zpgeek/job/detail.json?securityId=<id>&lid=<lid>` → `code 0`, `zpData.jobInfo.{postDescription, address, salaryDesc, experienceName, degreeName, locationName}`, `zpData.brandComInfo`, `zpData.bossInfo`.
- Salary is hidden for anonymous users in both list and DOM; the job detail page URL is `/job_detail/<encryptJobId>.html`.
- Page prerequisites: the page JS writes `__zp_stoken__`; the request must run inside a page on `www.zhipin.com` (headed browser or real browser), with header `x-requested-with: XMLHttpRequest`.

## D20 — Login parity with 拓岗宝 (user requirement, 2026-10-11)
Every platform that has login in 拓岗宝 must have login here too, because logged-in crawling returns better data.
拓岗宝 (v1.3.33 "platform-login-unify") logs into **BOSS直聘, 智联招聘, 前程无忧, 猎聘, 康强医疗网**; this project so far only had Boss, and has no 猎聘 / 康强 providers.
Target behaviour (borrowing 拓岗宝 v1.3.33): one login flow for all platforms — click → browser window opens the platform's login page → success detected automatically (cookie / probe) → window closes itself → session saved locally; a login-state check at startup and on demand; login state visible from the main UI (not buried in settings); login stays optional where anonymous crawling works (`auth = "optional"`), required only where the site demands it.
Per-platform details (login URL, success signal, what login unlocks, endpoint/transport changes) come from the research note `.trellis/tasks/10-11-djs-v1-1-0/research/platform-login.md`.

## D21 — Data completeness contract (user requirement, 2026-10-11)
A stored job is only "final data" when every CORE element is present; none may be missing.
Aligned with the jobcrawler schema (`raw_jobs`: title, company, city, district, description, education, experience,
salary_min/salary_max as integers) and 拓岗宝 `models.Job` (JobTitle, CompanyName, City, District, SalaryText/SalaryRange,
Education/Experience/MajorRequirement, JobDescription, PublishTimeText, SourceURL).

| Element | Column(s) | Rule |
|---|---|---|
| 职位名 | `title` | non-empty |
| 公司 | `company` | non-empty |
| 城市 | `city` (+ `district` when known) | non-empty |
| 薪资范围（数值） | `salary_min`, `salary_max` (integers) + `salary_unit` (元/月 · 元/天 · 元/时 · 元/年) + `salary_months` (e.g. 14) | numbers, never text; parsed from the platform's structured fields first, else from text (jobcrawler `parse_salary` rules: k/千 ×1000, 万 ×10000, each side its own unit, "·14薪" → months, not multiplied); 面议 = no numbers → incomplete |
| JD | `description` | plain text (HTML stripped), non-empty |
| Secondary (stored when the site has them) | `education`, `experience`, `source_url`, `published_at` | not gating |

Behaviour:
- The engine completes records: list pages first (results appear fast), then a detail hop **only for jobs still missing a core element**; a task is done when that pass is over. `detail`: `auto` (default) · `always` · `never`.
- Every job carries `missing` (list of absent core elements). Default outputs (UI results, CSV export, MCP `jobs_get`) show **complete jobs only**, with a visible count and reason for the incomplete ones (e.g. "12 条缺薪资：面议" / "Boss 未登录看不到薪资"), and an explicit switch to include them.
- Per-platform completeness % is part of the live audit and of the final report.
