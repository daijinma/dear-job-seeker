# v1.3.0 — crawl without Playwright

Baseline: v1.2.0 (`docs/plan/v1.2.0/`, decisions A19, A20, F36, F44). Only the change is written here.

Status 2026-10-16: E1 done (`e1-result.md`: BOSS ships list-only); built on branch `feat/plain-browser-crawl` as release **1.5.0**. Amendments of that day: A30–A32 in `docs/plan/v1.2.0/decisions.md`.

Build order (2026-10-13): v1.4.0 (self-update) is built first and releases as 1.4.0. This plan then releases under the next free number above it (1.5.0): an app only offers a release whose number is higher than its own.

## Why
- Playwright's Node.js driver is 65–70 % of every download (Windows 107 of 166 MB, macOS 136–139 of 195–201 MB unpacked).
- BOSS 直聘 and 猎聘 blank any page Playwright drives within 1–2 s (F36). The login window already avoids it (a plain browser read at the browser level). BOSS crawling still runs on Playwright: its list call (made right after load) works, its detail calls mostly fail (F44), so logged-in BOSS jobs carry salaries but no description and stay incomplete.

## Requirements
| # | Requirement |
|---|---|
| R1 | No Playwright in the app (the dev-only UI tests excepted, A30): not a dependency, not bundled, not imported. The browser is the installed Chrome, else Edge (every Windows 10/11 has Edge); a machine with neither gets a plain hint to install Chrome. |
| R2 | One browser client for crawling on the same plain-browser basis as the login window (DevTools websocket), with the interface providers use today: `get`/`post` answered by an in-page `fetch` as real `httpx.Response`s, `goto`, `eval`, `wait_cookie`, `cookies`. Headless where the platform allows it, a window where it needs one (BOSS). |
| R3 | BOSS: one search at the default pace returns its list with salaries (logged in) and the descriptions of a whole page of its jobs, each matched to its job, so its jobs can be complete. How the page is driven is decided by experiment E1 before the client is built (implement.md step 0). What ships depends on what is proven (assumed): list and details proven → full; only the list proven on its own → list-only (the detail call is never made, the jobs say "BOSS 暂时拿不到职位描述"); the list not proven or the result inconclusive → BOSS crawling is switched off and answers "BOSS 暂时不可用" before any page is opened. The release notes say which. |
| R4 | Login windows and crawling keep sharing one profile per platform; a session stored by one is read by the other. |
| R5 | One closing rule for every browser we start: one overall deadline for close → terminate → kill of the whole process tree (process group on macOS, `taskkill /T` on Windows), never an unbounded wait, and the profile opens again right after. A browser of the user's (`DJS_CDP_URL`) only loses the tab we opened. (Hardened for the 1.2.0 login window first, in 1.2.x.) |
| R6 | No size limit (user, 2026-10-16): the sizes are measured and reported, and only an abnormal size (far from the expected 25–35 MB zipped) is a defect; CI builds and smoke tests stay green; the smoke test no longer looks for Playwright's driver. |

## Not in this version
New platforms; UI changes; 智联 "both searches when logged in" (waits for the user's decision, F38).

## Acceptance
- No `playwright` import in `djs/`, not in the app's dependencies, not in the build; the UI browser tests (`tests/test_ui_browser.py`) keep it as a dev-only dependency (A30); full test suite green; CI: three builds + smoke green, zip sizes reported (no limit; an abnormal one is looked into).
- Live, at default pacing: 智联's PC search through `set_transport browser` returns complete jobs; BOSS (logged in only: it is never visited signed out, A24) returns its list and the descriptions of a whole page of jobs, each matched to its job, at the default pace — or R3 is reported as not met.
- A user without Chrome or Edge (simulated) gets the install hint, and every HTTP platform still works.

## Risks
| Risk | Answer |
|---|---|
| BOSS also detects a page-level DevTools session | E1 runs before any BOSS code, with controls (browser-level only / attach only / attach + evaluate); a captcha or business risk answer ends the round as inconclusive, never as a verdict |
| macOS users with neither Chrome nor Edge | the hint names Chrome; HTTP platforms are unaffected (only BOSS and optional transports need a browser) |
| Today's IP is under BOSS risk control after the 1.2.0 tests | E1 runs only after the user agrees, at least several hours later, one search |
