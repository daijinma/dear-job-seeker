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

## Findings from driving the real app (Peekaboo on the native window, fresh data folder)
| # | Finding | Status |
|---|---|---|
| F1 | 9-site search "Python 开发 · 杭州": all sites done in ~20 s, 411 jobs | ok |
| F2 | NowCoder returned 0 rows for any keyword with a space ("Python 开发"), 4 without it | fixed (keyword words joined) |
| F3 | 导出表格 opened the save dialog as a blank, detached, modal window; the whole app stopped responding | fix in progress (export to Downloads) |
| F4 | After 开始查找 the progress card sat below the fold and the top bar scrolled away | fix in progress (card under the search box, picker collapses, sticky header) |
| F5 | Default order "最新发现" bunched one site on top (all jobs found in the same minute) | fix in progress (interleave sites; add 薪资从高到低) |
| F6 | 公共招聘网 "会计 · 杭州" = 0 | not a bug: the site has few local postings ("会计 · 北京" = 17) |
| F7 | MCP server: a platform's worker thread could die at startup and leave its tasks pending forever | fixed (DB init once per process, worker loops never exit) |
