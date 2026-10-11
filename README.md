# dear-job-seeker

A **local job-search client** that speaks MCP. One process on your own machine crawls public job sites (and, optionally, sites that need your own login), keeps the results in a local SQLite file, and gives you two ways to use it:

- **MCP server (stdio)** — plug it into Claude Desktop / Claude Code / Cursor and ask for jobs in plain language.
- **Desktop app** — a small native window for people who never touch a terminal. No local web server, no open port.

> 中文简介：本地运行的找岗位客户端。AI 助手通过 MCP 调用，或直接打开桌面窗口使用；可按平台设置抓取频次与代理出口，需要登录的网站用你自己的账号在本机扫码登录，cookie 不上传。

| Search | Results |
|---|---|
| ![search](docs/design/ui-1-search.png) | ![results](docs/design/ui-2-results.png) |

## Quick start

```bash
git clone https://github.com/daijinma/dear-job-seeker.git && cd dear-job-seeker
python3 -m venv .venv
.venv/bin/pip install -e '.[ui]'            # '.[dev,ui]' to also run the tests
.venv/bin/playwright install chromium       # only needed for QR login (Boss)
```

**Desktop app**

```bash
.venv/bin/djs ui
```

**MCP client config** (Claude Desktop / Cursor / Claude Code `.mcp.json`)

```json
{"mcpServers": {"dear-job-seeker": {"command": "/abs/path/to/.venv/bin/djs"}}}
```

Then ask: *"Find Python jobs in Hangzhou on nowcoder and zhaopin, 2 pages each"*.

## Platforms

Each platform declares its own type and pacing; nothing is global.

| Platform | name | auth | city | interval (floor) | page cap | needs env |
|---|---|---|---|---|---|---|
| 牛客网 | `nowcoder` | public | optional | 0.6s (0.3s) | — | — |
| 国聘 | `iguopin` | public | optional | 3s (1s) | 20 | — |
| 新华英才 | `chinahr` | public | optional | 0.8s (0.5s) | — | — |
| 智联招聘 | `zhaopin` | public | optional | 3s (1.5s) | 3 | — |
| 公共招聘网 | `mohrss` | public | optional | 1s (0.5s) | — | — |
| 中智招聘 | `ciic` | public | optional | 3s (1.5s) | 20 | — |
| 字节跳动招聘 | `bytedance` | public | optional | 0.8s (0.4s) | — | — |
| 鱼泡直聘 | `yupao` | public | optional | 3s (1.5s) | 34 | `DJS_YUPAO_NONCE_KEY`, `DJS_YUPAO_SIGN_SALT` |
| 前程无忧 | `job51` | public | optional | 3s (1.5s) | 20 | `DJS_JOB51_HMAC_KEY` |
| 58同城 | `job58` | public | **required** | 1.5s (0.75s) | — | `DJS_JOB58_LUNA_SALT` |
| 赶集网 | `ganji` | public | **required** | 1.5s (0.75s) | 1 | — |
| BOSS 直聘 | `boss` | **login** | optional | 6s (4s) | 5 | — (QR login) |

- **auth** — `public` needs no account; `login` needs your own cookie, obtained by scanning a QR code in a real browser window on your machine.
- **interval (floor)** — default seconds between two requests of that platform, and the lowest value `set_frequency` accepts without `force=true`.
- Verified against the live sites on 2026-10-11: all `public` platforms returned real listings (the four "needs env" ones with keys supplied). Fixtures in `tests/` are synthetic; layouts and field names can change without notice.
- 实习僧 (shixiseng) is intentionally not included: its data needs a JavaScript evaluator.

### Platforms that need keys

Yupao, 51job and 58.com sign their requests with material extracted from their own mini-programs. **No key is shipped in this repository.** Supply them yourself as environment variables; a platform without its variables shows as "not configured" and is skipped.

```bash
# cp .env.example .env  (.env is git-ignored; never commit it)
DJS_YUPAO_NONCE_KEY=...
DJS_YUPAO_SIGN_SALT=...
DJS_JOB51_HMAC_KEY=...
DJS_JOB58_LUNA_SALT=...
```

Pass them through your MCP client's `env` block, or load them before the desktop app: `set -a; . ./.env; set +a; .venv/bin/djs ui`. Obtaining and using such keys is your own responsibility and may violate the platforms' terms.

### Boss (experimental)

QR login, cookie storage (0600, local only) and the list/detail calls are implemented, but Boss has been observed to answer the plain-HTTP list endpoint with `code 37` ("environment abnormal") even for valid logins. `code 37` is treated as a **block** (cooldown + retry), never as an expired login. Test it with your own account before relying on it.

## MCP tools

| Tool | Purpose |
|---|---|
| `provider_list` | every platform: label, auth, city rule, effective interval/jitter/max pages, floor, page cap, login/config state |
| `crawl_start(provider, keyword, city?, max_pages?)` | queue a crawl task (validates city rule and missing keys) |
| `task_list(status?, limit)` | task status and stats |
| `jobs_get(task_id?, provider?, limit, with_detail)` | stored jobs |
| `login(provider)` / `login_status` / `logout` | QR login for `auth=login` platforms; waiting tasks resume on success |
| `set_frequency(provider, interval?, jitter?, max_pages?, force?)` | per-platform override; below the floor needs `force=true` |
| `egress_add(url, provider="*")` / `egress_remove(url)` / `egress_list` | proxy pool (`direct` or `http(s)://…`; passwords are masked in output) |

Errors come back as `{"error": "..."}`; tools never raise.

## How it works

![core logic](docs/design/logic-a-core.png)

- **Queue** — SQLite tasks, claimed atomically; `pending → running → done | retry (backoff) | dead after 8 | waiting_login`. A worker thread runs inside the process; unfinished tasks continue on the next start.
- **Frequency** — effective value = your override, else the platform default, never below its floor unless forced. Spacing is enforced per *(platform, exit)* across threads and processes before every request.
- **Egress** — a platform-specific proxy beats the shared pool; the exit is sticky per task; blocked exits cool down 30 min (3 consecutive failures: 5 min). An empty pool means direct. Login platforms use direct unless you add a dedicated proxy for them (a foreign IP with your personal cookie is more likely to be flagged).
- **Dedupe** — `UNIQUE(provider, source_job_id)`; re-crawls update `last_seen_at`.
- Design docs: [`docs/plan/v1.1.0/prd.md`](docs/plan/v1.1.0/prd.md), [`docs/architecture.drawio`](docs/architecture.drawio), UI/logic mockups in [`docs/design/`](docs/design/).

## Desktop app

`djs ui` opens a native window (pywebview). The page talks to Python through an in-process bridge — there is no HTTP server and nothing listens on a port. Screens: search, results (CSV export), BOSS login, settings (speed: steady / standard / fast, account, optional proxy). Without pywebview, `djs ui` prints the install hint.

## Data and privacy

Everything lives in `$DJS_HOME` (default `~/.dear-job-seeker`): `djs.db` (SQLite), `profiles/<platform>/` (cookies 0600, browser profile), `pace/` (rate-limit locks), `ui_prefs.json`. Cookie values are never written to the database, logs, tool results or error messages.

## Development

```bash
.venv/bin/pip install -e '.[dev,ui]'
.venv/bin/python -m pytest -q          # no network, no browser, no window
```

Layout: `djs/{db,taskqueue,pacer,egress,engine,service}.py` (core), `djs/server.py` (MCP tools, thin wrappers over `service`), `djs/providers/` (one file per platform; contract in `base.py`), `djs/ui_app.py` + `djs/ui_api.py` + `djs/ui/` (desktop app).

## Compliance

Only crawl what you are allowed to, at low rates, from your own network; respect each site's terms and robots rules. Using a personal account for automated access (Boss) can get that account restricted. The authors accept no liability for how this tool is used.
