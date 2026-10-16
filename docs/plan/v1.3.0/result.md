# v1.3.0 result — built and released as 1.5.0 (2026-10-16)

Branch `feat/plain-browser-crawl`. Plan: `prd.md`, `design.md` (Client API), decisions A30–A32, F61–F64 in `docs/plan/v1.2.0/decisions.md`.

## What changed
`djs/browser.py` drives an installed Chrome or Edge over the DevTools protocol for crawling, as the login window already did: one flattened page session per visit, no `Page.enable` / `Runtime.enable`, in-page `fetch`, browser-level cookies, one reader thread per connection, every loop bounded. Playwright is gone from `djs/`, the dependencies and the build (it stays a dev-only dependency of the UI tests). `djs install-browser` is gone; nothing is downloaded.

| | 1.4.0 | 1.5.0 |
|---|---|---|
| unpacked (macOS arm64 app) | about 195 MB | 55 MB |
| zip (macOS arm64) | 74.7 MB | about 31 MB |

## Checks
- Suite: 1848 passed, 1 failed on the last run: `tests/test_ui_browser.py::test_empty_results_are_neutral_and_problems_offer_the_right_button`, which fails about 1 run in 8 on the 1.4.0 code too (8 runs each: 7 passed / 1 failed on both trees). The UI browser tests do not run in CI.
- Packaged app (macOS arm64): `--selftest` ok, smoke ok (MCP over stdio, one real crawl), the window starts in a throwaway home and writes the 12 assistant entries, the update check says 1.4.0 is not newer.
- **Real MCP client** (`tests/live/mcp_acceptance.py --djs <packaged djs>`, real data folder, BOSS not named): 13 of 13 steps; 数据分析 · 杭州 on 12 platforms started: 11 found, 1 no results, 230 jobs; 前程无忧's browser transport on a fresh folder: found, 50 complete jobs, no browser process left; a login window on a fresh folder opens, waits, and is gone after the client leaves.
- 智联 PC search with a new login: see F62. 前程无忧 browser transport: F61. BOSS with a new login: F63, F64.

## Not verified
Windows and Edge (read, not run); an Intel Mac; the real update from a published release (the next step: a 1.4.0 app updating itself to this release); BOSS detail calls (not made); the packaged app from `/Applications` on the final build.
