# E1 result — BOSS driven by a plain browser (2026-10-12)

Setup: Chrome 154.0.8037.98, headed, window 1280×900, the flags of the login window, a DevTools socket; one IP, about 17 hours after the last BOSS activity. Tabs by `Target.createTarget`, a flattened page session by `Target.attachToTarget`, `Page.navigate` (no `Page.enable`), in-page `fetch` by `Runtime.evaluate` (no `Runtime.enable`). Search `python`, 北京, page 1, default pace (8–14 s between calls). Each run used a fresh copy of the profile, deleted afterwards; the saved login itself was not touched. Every statement below is "this combination survived this round", not "undetected".

## Anonymous round (not repeated: A24)
A fresh profile, browser level only, 30 s: the page went through one security-check bounce and stayed on the search page; a token was written. The page showed the salary as `**-**元`. BOSS is never visited signed out from here on (A24), so B / C / function rounds were not run anonymously.

## Logged in (copy of the saved profile; page session attached, `location.href` read every second)
| Run | Result |
|---|---|
| 1: list + 3 details | list `code 0`, 15 jobs with salary text; details 3/3 `code 0`, each matched to its job by `encryptId` (and name), descriptions 108–496 characters; the page never left the search page; the token value was new on the second load |
| 2: list + up to 15 details | list `code 0`, 15 jobs; details 5/5 `code 0` and matched; the 6th (about 60 s after the load) answered `code 37` "您的环境存在异常" with a `seed` / `name` / `ts` body (a stale-token answer); stopped there, as the plan says; a reload afterwards gave a ready page and a new token (details were not retried) |

## What it supports
- The list call made right after the load works, with salaries, logged in.
- An attached, polled page was not blanked during about 65 s, and the details of up to 5 jobs came back matched to their jobs.
- Details stop working on one page after about a minute or 5 calls (not told apart yet): `code 37` means "this page's token is stale", so a detail needs a page loaded shortly before, as the design's per-call visit says. `code 37` stays a temporary block (cooldown), never an expired login.
- Control A (browser level only, after C) was not needed: C did not fail.

## Not proven
Whether a page load every 4 details (or per detail) returns a whole page of 15 matched descriptions at the default pace: full needs that. Until it is shown, BOSS stays list-only (prd R3). The next round costs about 3–4 more page loads on the account and waits for the user's go-ahead.
