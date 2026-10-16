# v1.4.0 — limit tests L1–L9 (2026-10-13)

Local parts: `tests/test_limits_local.py` (real engine, pacer, exit pool and queue against fake sites; fake clock, no network) and `tests/test_challenge.py`. Live parts: only what was safe without asking, written down as "not run" otherwise. A result is "this combination at this time", never a permanent fact.

| # | Local (CI) | Live |
|---|---|---|
| L1 pace floor | done: a site refusing a request that comes too fast (429 with and without `Retry-After`, 403): the exit rests, the cause stays in front (F47), nothing is sent during the rest, the pace never goes below a platform's floor without `force` (all 14 providers; tool, presets and `set_frequency`) | not run: it needs requests at half the default pace on a platform, platform by platform; wait for the user's go |
| L2 budget per hour | – | not run: sixty minutes of back-to-back searches on one public platform; wait for the user's go |
| L3 detail budget | done: a site that refuses the 4th detail: the pass stops at once, the list is kept, one cooldown, the retry asks only for the missing details, a site that never gives more ends the task after 8 refusals ≥ 30 min apart | not run (前程无忧, 新华英才, 58同城 to the end of their details); wait for the user's go |
| L4 page caps | – | not run: caps are only known from notes (智联 60, 国聘 400, 中智 400, 前程无忧 1000, 猎聘 80, 赶集 8 a page); needs one request past each cap |
| L5 session dies mid-crawl | done for every platform that has a login, by `auth` (so a platform added later is covered): the task waits for a login, not one request goes out signed out, resumes after a login. Optional platforms: the dead session is dropped and the next try goes on signed out, by design (`engine._login_expired`) | not run (康强 with a copy of its profile) |
| L6 challenge page | done, and it found defects (below) | not run: what each public platform answers when asked too fast |
| L7 exits | done: one dead exit of two is rested and the other serves, only exit dead → `retrying` with the cause, recovery | **run 2026-10-16** (below): 11 public platforms, direct and through the Clash port, identical; Clash sends every job site direct |
| L8 long run | done, compressed: 200 searches, 600 tasks, a cooldown that ends, a cookie that expires, a login that resumes 80 waiting tasks; no thread, file or stuck task grows | not run: 8 h unattended |
| L9 Windows | `update-check.sh` and the smoke test on the CI runner (first run with the tag) | none: no Windows machine |

One live fact from today: two searches (测试工程师 · 成都 on 13 sites, 数据分析 · 杭州 on 12) at default pace, BOSS left out: 0 failures, no cooldown (`check-1.2.6.md`, `tests/live/mcp_acceptance.py`).

## Defects the local tests found, and what was done
| # | Defect | Result |
|---|---|---|
| D1 | A challenge page (HTTP 200 with a slider / captcha / 访问验证 page) was read by 12 of 14 providers as a parse error: 8 requests in about 14 minutes (three within 7 s), and the user was told 「网站页面变了」 | fixed centrally in `djs/providers/base.py`: a parse failure whose body looks like a challenge becomes `Blocked` (30-minute cooldown, 「被网站暂时限制」); a page that parses is never inspected, a larger page or one without markers stays a structure error |
| D2 | A challenge page on a detail request did not stop the pass: every remaining job was asked (145 on 前程无忧) | fixed with D1: one cooldown, the rest stay incomplete with a retry coming |
| D3 | After a task gave up on refused details, the jobs said 「平台未提供职位描述」, blaming the platform | now 「网站拒绝了详情请求」 |
| D4 | A detail answering 404/410 ended the whole pass and retried the task | now that job says 「职位已下架」 and the pass goes on; a later search asks for it again, as for any detail that failed (a 404 may also be a changed address, so it is not made final) |

Markers (lower-cased match, only on a response the provider could not parse): 验证码, 滑动验证, 滑块验证, 安全验证, 访问验证, 人机验证, 请完成验证, 请按住滑块, captcha, geetest, "verify you are human", "just a moment", `_____tmd_____`, `/punish`. Size limit: 30,000 characters for a page, 2,000 for a JSON body (a refusal sent as JSON is a line or two; a larger one holds job texts, which often say 验证码); a JSON body is read with its `\uXXXX` escapes decoded. Known gap: a verification page that carries none of the markers (e.g. a script that only sets a cookie) is still read as a parse error.

## L7 live, 2026-10-16: direct against the proxy app, `tests/live/exits_compare.py`
Setup on this machine: Clash Verge (mihomo 1.19) in rule mode with **TUN on** (default route split into `utun1024`), `mixed-port` 7897, one node (a Korean VPS). Rules: private and Chinese address ranges (`cncidr`) → DIRECT, everything else → the node. The app's own "direct" exit does not read the system proxy (`trust_env=False`), but the TUN catches it, so it reaches Clash like any other program; the exit `http://127.0.0.1:7897` reaches Clash through its port.

Run: 测试工程师 · 成都, default pace, a fresh data folder per run (only the signing keys copied, deleted after), 11 platforms (not BOSS 直聘, 猎聘 or 康强; no login on a fresh folder), direct first, the proxy run 5 minutes later; then 7 of them again with Clash's own log recorded.

| platform | direct | through 127.0.0.1:7897 |
|---|---|---|
| 字节跳动招聘, 国聘, 牛客网, 鱼泡直聘, 智联招聘, 赶集网 | found 2 / 20 / 16 / 15 / 20 / 8, each in under 15 s | the same counts, the same seconds |
| 公共招聘网 (mohrss) | no_results (nothing for this query), 5 s | the same |
| 中智招聘 | found 20, 71 s | the same |
| 前程无忧 | found 50, 160 s | the same |
| 58同城 | found 50, 86 s | the same |
| 新华英才 | found 50, 50 s | the same |

- No refusal, cooldown or failed task in either run; the exits table of `status` after each run showed 7 requests ok on `direct` in one and 7 ok on `http://127.0.0.1:7897` (and none on `direct`) in the other, so the proxy run did use the proxy exit.
- **Which rule each site takes** (read from Clash's own log while the app ran, 7 hosts, and from Clash's DNS answers checked against its `cncidr` list, the other hosts and the three login platforms): all job hosts resolve to Chinese addresses and match `RuleSet(cncidr) → DIRECT`; none goes through the Korean node. Only a non-Chinese address would (`ipinfo.io`, used to ask the exit's country, answered KR).
- So, here, the two exits are the same path and give the same results; the sites see this machine's own Chinese address. A foreign address is not the cause of BOSS 直聘's code 37 on 2026-10-12 (its hosts match the same rule); a provider added later whose host resolves abroad would leave through the Korean node, and `login` platforms are pinned to `direct` by design (`egress._pool`) but are still caught by the TUN.
- The Clash controller was reachable all along: it listens on a unix socket (`/var/run/clash-verge-service/users/501/verge-mihomo.sock`), not on the TCP port the earlier probe tried.
- Not measured: an exit that really leaves through a foreign node (no job host goes there, so no such result exists), and the same run on another day or network.
