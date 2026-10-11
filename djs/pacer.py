"""Request frequency: effective settings (override ?? class default) and the cross-thread/process spacer."""
from __future__ import annotations

import hashlib
import random
import time

from djs import filelock, providers
from djs.db import home

DEFAULT_MAX_PAGES = 3


def effective(conn, provider: str) -> tuple[float, float, int]:
    prov = providers.get(provider)
    row = conn.execute("SELECT * FROM provider_settings WHERE provider=?", (provider,)).fetchone()
    pick = lambda col, default: default if row is None or row[col] is None else row[col]  # noqa: E731
    return pick("interval", prov.interval), pick("jitter", prov.jitter), pick("max_pages", DEFAULT_MAX_PAGES)


def set_frequency(conn, provider: str, interval=None, jitter=None, max_pages=None, force: bool = False) -> dict:
    prov = providers.get(provider)
    if interval is not None and interval < prov.min_interval and not force:
        raise ValueError(f"interval {interval} < min_interval {prov.min_interval} for {provider}; pass force=True to override")
    if any(v is not None and v < 0 for v in (interval, jitter)) or (max_pages is not None and max_pages < 1):
        raise ValueError("interval/jitter must be >= 0 and max_pages >= 1")
    conn.execute(
        """INSERT INTO provider_settings(provider, interval, jitter, max_pages) VALUES (?,?,?,?)
           ON CONFLICT(provider) DO UPDATE SET interval=COALESCE(excluded.interval, interval),
             jitter=COALESCE(excluded.jitter, jitter), max_pages=COALESCE(excluded.max_pages, max_pages)""",
        (provider, interval, jitter, max_pages))
    i, j, m = effective(conn, provider)
    return {"provider": provider, "interval": i, "jitter": j, "max_pages": m}


def transport(conn, provider: str) -> str:
    """How the platform is captured: the user's override while the provider still supports it, else its default."""
    prov = providers.get(provider)
    row = conn.execute("SELECT transport FROM provider_settings WHERE provider=?", (provider,)).fetchone()
    return row["transport"] if row and row["transport"] in prov.transports else prov.transports[0]


def set_transport(conn, provider: str, transport: str | None = None) -> dict:
    """Override how the platform is captured; None clears the override."""
    prov = providers.get(provider)
    if transport is not None and transport not in prov.transports:
        raise ValueError(f"{provider} supports transport(s) {', '.join(prov.transports)}, not {transport!r}")
    conn.execute("INSERT INTO provider_settings(provider, transport) VALUES (?,?) "
                 "ON CONFLICT(provider) DO UPDATE SET transport=excluded.transport", (provider, transport))
    return {"provider": provider, "transport": transport or prov.transports[0]}


def wait(provider: str, egress_id: str, interval: float, jitter: float) -> None:
    """Serialize requests per (provider, egress): the lock file holds the last request time (wall clock,
    shared across processes); the lock is held while sleeping so waiters queue up."""
    d = home() / "pace"
    d.mkdir(exist_ok=True)
    path = d / hashlib.sha1(f"{provider}:{egress_id}".encode()).hexdigest()
    with open(path, "a+") as f, filelock.locked(f):
        f.seek(0)
        last = float(f.read() or 0)
        time.sleep(max(0.0, last + interval + random.uniform(0, jitter) - time.time()))
        f.seek(0)
        f.truncate()
        f.write(str(time.time()))
