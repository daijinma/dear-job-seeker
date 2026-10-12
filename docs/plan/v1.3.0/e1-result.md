# E1 result — BOSS driven by a plain browser (2026-10-12)

Setup: Chrome 154.0.8037.98, headed, window 1280×900, the flags of the login window, a DevTools socket; one IP, about 17 hours after the last BOSS activity. Tabs by `Target.createTarget`, a flattened page session by `Target.attachToTarget`, `Page.navigate` (no `Page.enable`), in-page `fetch` by `Runtime.evaluate` (no `Runtime.enable`). Search `python`, 北京, page 1, default pace (8–14 s between calls). Each run used a fresh copy of the profile, deleted afterwards; the saved login itself was not touched. Every statement below is "this combination survived this round", not "undetected".

## Anonymous round (not repeated: A24)
A fresh profile, browser level only, 30 s: the page went through one security-check bounce and stayed on the search page; a token was written. The page showed the salary as `**-**元`. BOSS is never visited signed out from here on (A24), so B / C / function rounds were not run anonymously.

## Logged in (copy of the saved profile; page session attached, `location.href` read every second)
| Run | Result |
|---|---|
| 1: list + 3 details | list `code 0`, 15 jobs with salary text; details 3/3 `code 0`, each matched to its job by `encryptId` (and name), descriptions 108–496 characters; the page never left the search page; the token value was new on the second load |
| 2: list + up to 15 details | list `code 0`, 15 jobs; details 5/5 `code 0` and matched; the 6th (about 60 s after the load) answered `code 37` "您的环境存在异常" with a `seed` / `name` / `ts` body: BOSS's risk-control answer; stopped there, as the plan says (a risk answer ends the round as inconclusive); a reload afterwards gave a ready page and a new token (details were not retried) |

## What it supports
- The list call made right after the load works, with salaries, logged in.
- An attached, polled page was not blanked during about 65 s, and the details of up to 5 jobs came back matched to their jobs.
- Details stop working on one page after about a minute or 5 calls. `code 37` is BOSS's risk-control answer and is treated as one (temporary block, cooldown), never as an expired login. Its body looks like a token challenge, but that was not tested and says nothing about whether a fresh token would have been accepted.
- Control A (browser level only, after C) was not needed: C did not fail.

## Verdict (prd R3)
The list is proven on its own; the details ended in a risk answer. So BOSS ships **list-only**: salaries when logged in, no detail call by the crawl, the reason "BOSS 暂时拿不到职位描述" on its jobs (A21 stands). No further detail rounds: each one costs the user's account and IP hours of risk control, and a whole page of descriptions would need several more loads. Revisit only if the user asks for it, hours after the last BOSS activity.
