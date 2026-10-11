# dear-job-seeker

Local MCP job crawler (stdio). One process: MCP server + in-process queue worker. Public sites plus login-required sites (Boss) using your own cookie.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/playwright install chromium
```

MCP client config (Claude Desktop / Cursor / Claude Code `.mcp.json`):

```json
{"mcpServers": {"dear-job-seeker": {"command": "/abs/path/.venv/bin/djs"}}}
```

## Desktop app (for non-technical users)

```bash
.venv/bin/pip install -e '.[ui]'
.venv/bin/djs ui
```

Opens a native window (pywebview). There is no local web port or HTTP server: the page talks to Python through the window's built-in bridge. It shares the queue worker and SQLite with the MCP server. Four screens: search, results (CSV export), BOSS login, settings (speed: steady / standard / fast, account, optional proxy list). The stdio MCP server (plain `djs`) is the interface for AI clients.

## Tools

| Tool | Purpose |
|---|---|
| `provider_list` | platforms: kind, auth, effective frequency, min_interval, page_cap, login state |
| `crawl_start(provider, keyword?, city?, tenant?, max_pages?)` | queue a task |
| `task_list(status?, limit)` / `jobs_get(task_id?, provider?, limit, with_detail)` | read results |
| `login` / `login_status` / `logout` `(provider)` | browser QR login; tasks in `waiting_login` resume on success |
| `set_frequency(provider, interval, jitter, max_pages, force)` | override pacing; below `min_interval` needs `force=true` |
| `egress_add(url, provider="*")` / `egress_remove(url)` / `egress_list` | proxy pool (`direct` or http(s) URL) |

Errors are returned as `{"error": "..."}`.

## Platforms

| name | kind | auth | interval (min) | pages |
|---|---|---|---|---|
| nowcoder | keyword | public | 0.6s (0.3) | no cap |
| iguopin | keyword | public | 3.0s (1.0) | cap 20 |
| chinahr | keyword | public | 0.8s (0.5) | no cap |
| boss | keyword | login | 6.0s (4.0) | cap 5 |

`kind`: `keyword` = search by keyword (+city); `tenant` = one company site per task. `auth`: `login` platforms need `login` first.

## Frequency model

Effective = `set_frequency` override, else the provider default. Spacing is enforced per (provider, egress) across threads and processes (file lock under `pace/`), before every HTTP request. Default `max_pages` is 3, further limited by the platform `page_cap`.

Egress: a provider-dedicated egress wins over the shared (`*`) pool; the exit is sticky per task; blocked exits cool down 30 min (3 consecutive failures: 5 min). Empty pool means `direct`. Login providers use `direct` unless you add a dedicated egress for them.

## Task lifecycle

`pending -> running -> done | pending (retry, exponential backoff) | dead (after 8 attempts) | waiting_login`. The worker thread lives and dies with the MCP process; unfinished tasks continue next start.

## Boss status: experimental

QR login and cookie storage are implemented, but the plain-HTTP list endpoint has been observed to answer `code 37` (risk control) even for valid logins. Such responses are treated as a block (cooldown + retry), never as an expired login. Verify on your own account before relying on it; public sites (nowcoder, iguopin, chinahr) are the verified path.

## Compliance and ban risk

Boss uses your personal account cookie; automated access violates the site terms and can get the account restricted. Keep the default pacing, crawl small, use `direct` egress from your own network, and do not share the data directory. Public sites: respect their terms and robots rules.

## Data dir

`$DJS_HOME` or `~/.dear-job-seeker`: `djs.db` (SQLite, WAL), `profiles/<provider>/` (cookies 0600, browser profile), `pace/` (rate-limit lock files).
