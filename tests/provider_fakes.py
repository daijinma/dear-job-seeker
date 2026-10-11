"""Test doubles shared by the provider tests: an httpx client with a canned answer, and a stand-in for
djs.browser.BrowserClient (the requests of an httpx client under the same names, plus the page primitives)."""
from types import SimpleNamespace

import httpx

from djs import normalize, service, taskqueue


def stored_jobs(conn, task_id=None, provider=None, with_detail=False) -> list[dict]:
    """Every stored job, newest first, as whole rows the way service.job_detail shows one (`missing` a list, `complete` a
    bool, `incomplete_reason`): what the engine tests read back. The jobs_get tool shortens and merges; this does not."""
    rows = conn.execute(
        f"SELECT {service.JOB_COLS}{', detail' if with_detail else ''} FROM jobs j "
        "WHERE (? IS NULL OR j.id IN (SELECT job_id FROM task_jobs WHERE task_id=?)) AND (? IS NULL OR j.provider=?) "
        "ORDER BY j.id DESC", (task_id, task_id, provider, provider)).fetchall()
    return service.explain([service.load_job(r) for r in rows])


def client(body=None, status=200, text=None, content=None, seen=None, **kwargs) -> httpx.Client:
    """An httpx client whose every request is answered with the given JSON `body`, `text` or raw `content`."""
    def handler(request):
        if seen is not None:
            seen.append(request)
        if content is not None:
            return httpx.Response(status, content=content)
        return httpx.Response(status, text=text) if text is not None else httpx.Response(status, json=body)
    return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)


def task(conn, provider, keyword, city=None, status="done"):
    """A task row of the search (keyword, city) in the given status."""
    tid = taskqueue.create(conn, provider, {"keyword": keyword, **({"city": city} if city else {})})
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    return tid


def job(conn, provider, sid, tasks, seen=1000.0, **cols):
    """A stored job linked to `tasks`; `complete=1` unless the test says otherwise (the page lists complete jobs by
    default). The columns that follow from the others (pay per month, fingerprint) are derived like the crawl does."""
    cols = {"title": sid, "company": None, "city": None, "salary_text": None, "description": None,
            "source_url": f"https://x.test/{sid}", "complete": 1, "missing": "", **cols}
    cols = {**normalize.monthly(cols), "fingerprint": normalize.fingerprint(cols), **cols}
    cur = conn.execute(
        f"INSERT INTO jobs(provider, source_job_id, first_seen_at, {', '.join(cols)}) VALUES (?,?,?,{','.join('?' * len(cols))})",
        (provider, sid, seen, *cols.values()))
    conn.executemany("INSERT OR IGNORE INTO task_jobs VALUES (?,?)", [(t, cur.lastrowid) for t in tasks])
    return cur.lastrowid


class FakeBrowser:
    """get/post go through a canned httpx client; goto, eval and wait_cookie only record what the provider asked for.
    `cookies` is what the page holds ({name: value}), `href` what location.href answers, `cookie_timeout` makes
    wait_cookie give up the way BrowserClient does (httpx.ReadTimeout)."""

    def __init__(self, body=None, status=200, text=None, seen=None, cookies=None, href="https://site.test/",
                 cookie_timeout=False):
        self.http = client(body, status, text, seen=seen)
        jar = [{"name": name, "value": value} for name, value in (cookies or {}).items()]
        rec = lambda name: (lambda *a, **k: self.calls.append((name, a[0] if a else k.get("name"))))  # noqa: E731
        self.page = SimpleNamespace(goto=rec("page.goto"), wait_for_load_state=rec("page.settle"),
                                    context=SimpleNamespace(cookies=lambda url: jar, clear_cookies=rec("clear_cookie")))
        self.calls, self.href, self.cookie_timeout = [], href, cookie_timeout

    def get(self, url, paced=True, **kwargs):
        return self.http.get(url, **kwargs)

    def post(self, url, **kwargs):
        return self.http.post(url, **kwargs)

    def goto(self, url, timeout=30):
        self.calls.append(("goto", url))

    def eval(self, js, arg=None):
        self.calls.append(("eval", js))
        return self.href

    def wait_cookie(self, name, timeout=10):
        self.calls.append(("wait_cookie", name))
        if self.cookie_timeout:
            raise httpx.ReadTimeout("cookie never came")
