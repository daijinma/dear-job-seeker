"""Egress pool: which exit a task uses, its health/cooldown, and the paced httpx client."""
from __future__ import annotations

import hashlib
import time
from urllib.parse import urlsplit

import httpx

from djs import pacer, providers

BLOCK_COOLDOWN = 30 * 60
FAIL_COOLDOWN = 5 * 60
FAIL_STREAK_LIMIT = 3
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class NoEgressAvailable(Exception):
    pass


def mask(url: str) -> str:
    """Hide the proxy password for anything shown to the user: http://u:pw@h:1 -> http://u:***@h:1."""
    u = urlsplit(url)
    if u.password is None:
        return url
    return url.replace(f":{u.password}@", ":***@", 1)


def add(conn, url: str, provider: str = "*") -> dict:
    if url != "direct" and not url.startswith(("http://", "https://")):
        raise ValueError("egress url must be 'direct' or http(s)://...")
    if provider != "*":
        providers.get(provider)  # KeyError on typos instead of a silently unused dedicated row
    conn.execute("INSERT INTO egress(id, provider) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET provider=excluded.provider, enabled=1",
                 (url, provider))
    return {"url": mask(url), "provider": provider}


def remove(conn, url: str) -> int:
    """`url` may be the raw URL or the masked form shown by list_."""
    ids = [r["id"] for r in conn.execute("SELECT id FROM egress") if r["id"] == url or mask(r["id"]) == url]
    return sum(conn.execute("DELETE FROM egress WHERE id=?", (i,)).rowcount for i in ids)


def list_(conn) -> list[dict]:
    # health rows are keyed by the provider that used the exit, so a shared ('*') exit has one per provider
    rows = conn.execute(
        """SELECT e.id, e.provider, e.enabled, COALESCE(MAX(h.blocked_until), 0) AS blocked_until,
                  COALESCE(SUM(h.total_ok), 0) AS total_ok, COALESCE(SUM(h.total_fail), 0) AS total_fail
           FROM egress e LEFT JOIN egress_health h ON h.egress_id=e.id
           GROUP BY e.id ORDER BY e.id""").fetchall()
    return [{**dict(r), "id": mask(r["id"])} for r in rows]


def _pool(conn, provider: str) -> list[str]:
    ids = lambda p: [r["id"] for r in conn.execute(  # noqa: E731
        "SELECT id FROM egress WHERE provider=? AND enabled=1 ORDER BY id", (p,))]
    dedicated = ids(provider)
    if dedicated:
        return dedicated
    if providers.get(provider).auth == "login":
        return ["direct"]  # personal cookie + foreign IP triggers risk control
    return ids("*") or ["direct"]


def lease(conn, provider: str, key: str) -> str:
    now = time.time()
    blocked = {r["egress_id"] for r in conn.execute(
        "SELECT egress_id FROM egress_health WHERE provider=? AND blocked_until>?", (provider, now))}
    usable = [e for e in _pool(conn, provider) if e not in blocked]
    if not usable:
        raise NoEgressAvailable(f"all egress for {provider} are cooling down")
    h = int(hashlib.md5(f"{provider}:{key}".encode()).hexdigest(), 16)
    return usable[h % len(usable)]


def report(conn, exit_id: str, provider: str, ok: bool, blocked: bool = False, retry_after: float | None = None):
    row = conn.execute("SELECT fail_streak FROM egress_health WHERE egress_id=? AND provider=?",
                       (exit_id, provider)).fetchone()
    streak = 0 if ok else (row["fail_streak"] if row else 0) + 1
    cooldown = 0.0
    if blocked:
        cooldown = max(BLOCK_COOLDOWN, retry_after or 0)
    elif streak >= FAIL_STREAK_LIMIT:
        cooldown, streak = FAIL_COOLDOWN, 0
    now = time.time()
    conn.execute(
        """INSERT INTO egress_health(egress_id, provider, blocked_until, fail_streak, last_used, total_ok, total_fail)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(egress_id, provider) DO UPDATE SET
             blocked_until=CASE WHEN ?>0 THEN ? ELSE blocked_until END, fail_streak=excluded.fail_streak,
             last_used=excluded.last_used, total_ok=total_ok+excluded.total_ok, total_fail=total_fail+excluded.total_fail""",
        (exit_id, provider, now + cooldown if cooldown else 0, streak, now, int(ok), int(not ok),
         cooldown, now + cooldown))


class PacedTransport(httpx.BaseTransport):
    def __init__(self, inner: httpx.BaseTransport, provider: str, exit_id: str, interval: float, jitter: float):
        self.inner, self.args = inner, (provider, exit_id, interval, jitter)

    def handle_request(self, request):
        pacer.wait(*self.args)
        return self.inner.handle_request(request)

    def close(self):
        self.inner.close()


def make_client(exit_id: str, provider, interval: float, jitter: float, cookies=None, transport=None) -> httpx.Client:
    """`transport` is only for tests (httpx.MockTransport)."""
    inner = transport or httpx.HTTPTransport(proxy=None if exit_id == "direct" else exit_id)
    paced = PacedTransport(inner, provider.name, exit_id, interval, jitter)
    return httpx.Client(transport=paced, trust_env=False, timeout=20, cookies=cookies, headers={"User-Agent": UA})
