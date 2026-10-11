"""djs.results: the jobs of one search, the plain text of third-party descriptions, friendly error wording."""
import pytest

from djs import db, results, service, taskqueue


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    c = db.connect()
    yield c
    c.close()


def task(conn, provider, keyword, city=None, status="done"):
    tid = taskqueue.create(conn, provider, {"keyword": keyword, **({"city": city} if city else {})})
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    return tid


def job(conn, provider, sid, tasks, seen=1000.0, **cols):
    cols = {"title": sid, "company": None, "city": None, "salary_text": None, "description": None,
            "source_url": f"https://x.test/{sid}", **cols}
    cur = conn.execute(
        f"INSERT INTO jobs(provider, source_job_id, first_seen_at, {', '.join(cols)}) VALUES (?,?,?,{','.join('?' * len(cols))})",
        (provider, sid, seen, *cols.values()))
    conn.executemany("INSERT OR IGNORE INTO task_jobs VALUES (?,?)", [(t, cur.lastrowid) for t in tasks])
    return cur.lastrowid


@pytest.fixture
def seeded(conn):
    """Search ('py', '杭州'): nowcoder + chinahr, and the nowcoder task run twice. Other searches must stay out."""
    nc1, ch, nc2 = (task(conn, "nowcoder", "py", "杭州"), task(conn, "chinahr", "py", "杭州"),
                    task(conn, "nowcoder", "py", "杭州"))
    ids = {
        "a": job(conn, "nowcoder", "a", [nc1, nc2], seen=100, company="乙公司", title="A"),   # found again by the repeat
        "b": job(conn, "nowcoder", "b", [nc1, nc2], seen=300, company="甲公司", title="B"),
        "c": job(conn, "chinahr", "c", [ch], seen=200, company=None, title="C"),
        "d": job(conn, "nowcoder", "d", [nc2], seen=400, company="丙公司", title="D"),
    }
    other_city = task(conn, "nowcoder", "py", "上海")
    nationwide = task(conn, "nowcoder", "py")
    other_word = task(conn, "nowcoder", "go", "杭州")
    ids["sh"] = job(conn, "nowcoder", "sh", [other_city], title="SH")
    ids["any"] = job(conn, "nowcoder", "any", [nationwide], title="ANY")
    ids["go"] = job(conn, "nowcoder", "go", [other_word], title="GO")
    return ids


def titles(page):
    return [j["title"] for j in page["jobs"]]


# ---- search_jobs ----
def test_a_search_spans_every_task_and_lists_each_job_once(conn, seeded):
    page = results.search_jobs(conn, "py", "杭州")
    assert sorted(titles(page)) == ["A", "B", "C", "D"] and page["total"] == 4
    assert page["task_ids"] == [1, 2, 3] and page["searching"] is False


def test_per_site_counts_cover_the_whole_search_in_registry_order(conn, seeded):
    page = results.search_jobs(conn, "py", "杭州")
    assert page["per_site"] == [{"provider": "nowcoder", "label": "牛客网", "count": 3},
                                {"provider": "chinahr", "label": "新华英才", "count": 1}]
    filtered = results.search_jobs(conn, "py", "杭州", provider="chinahr")
    assert titles(filtered) == ["C"] and filtered["total"] == 1
    assert filtered["per_site"] == page["per_site"]   # the chips do not shrink while one is selected


def test_a_search_without_city_is_its_own_group(conn, seeded):
    for city in (None, ""):
        assert titles(results.search_jobs(conn, "py", city)) == ["ANY"]
    assert titles(results.search_jobs(conn, "py", "上海")) == ["SH"]
    assert titles(results.search_jobs(conn, "go", "杭州")) == ["GO"]
    empty = results.search_jobs(conn, "nothing", "杭州")
    assert empty == {"jobs": [], "total": 0, "per_site": [], "searching": False, "task_ids": []}


def test_pagination_keeps_total_and_never_repeats_or_skips(conn, seeded):
    pages = [results.search_jobs(conn, "py", "杭州", limit=3, offset=o) for o in (0, 3)]
    assert [len(p["jobs"]) for p in pages] == [3, 1] and {p["total"] for p in pages} == {4}
    assert sorted(titles(pages[0]) + titles(pages[1])) == ["A", "B", "C", "D"]
    assert results.search_jobs(conn, "py", "杭州", offset=10)["jobs"] == []
    assert len(results.search_jobs(conn, "py", "杭州", limit=0)["jobs"]) == 1   # limit is clamped to at least 1


def test_sort_new_is_newest_discovery_first_and_stable(conn, seeded):
    assert titles(results.search_jobs(conn, "py", "杭州")) == ["D", "B", "C", "A"]
    same_second = [job(conn, "chinahr", f"t{i}", [2], seen=500, title=f"T{i}") for i in range(3)]
    assert titles(results.search_jobs(conn, "py", "杭州", limit=3)) == ["T0", "T1", "T2"]   # a page keeps its own order
    assert len(same_second) == 3


def test_sort_company_groups_by_company_and_puts_unknown_last(conn, seeded):
    page = results.search_jobs(conn, "py", "杭州", sort="company")
    assert [j["company"] for j in page["jobs"]] == sorted(["乙公司", "甲公司", "丙公司"]) + [None]
    with pytest.raises(ValueError, match="排序"):
        results.search_jobs(conn, "py", "杭州", sort="id; DROP TABLE jobs")


def test_complete_only_leaves_out_jobs_missing_a_core_element(conn):
    t = task(conn, "nowcoder", "py")
    full = {"title": "T", "company": "C", "city": "杭州", "salary_text": "20-30K", "description": "D"}
    job(conn, "nowcoder", "ok", [t], **full)
    for field in full:   # each core element absent (NULL) or empty, in turn
        job(conn, "nowcoder", f"null-{field}", [t], **{**full, field: None})
        job(conn, "chinahr", f"empty-{field}", [t], **{**full, field: ""})
    everything = results.search_jobs(conn, "py", None)
    assert everything["total"] == 11 and [s["count"] for s in everything["per_site"]] == [6, 5]   # the default shows all
    page = results.search_jobs(conn, "py", None, complete_only=True)
    assert titles(page) == ["T"] and page["total"] == 1   # rows and counts agree
    assert page["per_site"] == [{"provider": "nowcoder", "label": "牛客网", "count": 1}]
    assert results.search_jobs(conn, "py", None, provider="chinahr", complete_only=True)["total"] == 0
    assert results.search_jobs(conn, "py", None, complete_only=False)["total"] == 11


def test_job_rows_carry_label_and_flag_not_the_description_text(conn):
    t = task(conn, "nowcoder", "py")
    job(conn, "nowcoder", "x", [t], description="很长的岗位介绍", salary_text="20-30K", city="杭州")
    job(conn, "nowcoder", "y", [t], description="", seen=2000)
    job(conn, "gone", "z", [t], seen=3000)   # a platform that no longer exists
    rows = results.search_jobs(conn, "py", None)["jobs"]
    by_title = {j["title"]: j for j in rows}
    assert by_title["x"]["has_description"] is True and by_title["y"]["has_description"] is False
    assert all("description" not in j for j in rows)
    assert (by_title["x"]["label"], by_title["z"]["label"]) == ("牛客网", "gone")
    assert set(by_title["x"]) == {"id", "provider", "label", "title", "company", "city", "salary_text", "source_url",
                                  "published_at", "first_seen_at", "has_description"}


def test_searching_follows_the_status_of_any_task_of_the_search(conn):
    done, queued = task(conn, "nowcoder", "py"), task(conn, "chinahr", "py", status="pending")
    assert results.search_jobs(conn, "py", None)["searching"] is True
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (queued,))
    assert results.search_jobs(conn, "py", None)["searching"] is True
    conn.execute("UPDATE tasks SET status='waiting_login' WHERE id=?", (queued,))
    assert results.search_jobs(conn, "py", None)["searching"] is False   # waiting for the user is not searching
    assert done and results.search_jobs(conn, "py", None)["task_ids"] == [done, queued]


def test_description_is_the_stored_text_or_none(conn):
    t = task(conn, "nowcoder", "py")
    full, empty = job(conn, "nowcoder", "f", [t], description="全文"), job(conn, "nowcoder", "e", [t], description="")
    assert (results.description(conn, full), results.description(conn, empty), results.description(conn, 999)) == ("全文", None, None)


# ---- untrusted text ----
@pytest.mark.parametrize("raw, want", [
    ("###Python开发职位描述<br/><br/>岗位职责：<br>1. 开发<br />2. 测试", "###Python开发职位描述\n\n岗位职责：\n1. 开发\n2. 测试"),
    ("<p>一</p><p>二</p><div>三</div><ul><li>四</li><li>五</li></ul>", "一\n二\n三\n四\n五"),
    ("岗位职责：<BR>1. 开发<P>2. 测试</P>", "岗位职责：\n1. 开发2. 测试"),
    ('<img src=x onerror="window.__pwned=1">岗位', "岗位"),
    ('<a href="javascript:alert(1)" onclick="x()">链接</a><svg onload=alert(1)>字</svg>', "链接字"),
    ("ok<script>alert(1)</script><style>p{color:red}</style>end", "okend"),
    ("<!-- 备注 -->正文", "正文"),
    ("A &amp; B &lt;b&gt;x&lt;/b&gt;&nbsp;C", "A & B <b>x</b> C"),   # an escaped tag stays text: textContent shows it as text
    ("薪资 > 20K, 1<2 and 3 < 4", "薪资 > 20K, 1<2 and 3 < 4"),
    ("a\r\n\r\n\r\n\r\nb   \n c", "a\n\nb\n c"),
    ("  \n 开头和结尾 \n  ", "开头和结尾"),
    (None, ""), ("", ""), (123, "123"),
])
def test_plain_text_keeps_lines_and_drops_every_tag(raw, want):
    assert results.plain_text(raw) == want


def test_plain_text_leaves_nothing_that_could_run_as_markup():
    evil = ('<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script><iframe src="javascript:1"></iframe>'
            '<svg/onload=window.__pwned=3><body onload=x()>文本')
    out = results.plain_text(evil)
    assert out == "文本" and "<" not in out and "onerror" not in out


# ---- friendly errors ----
@pytest.mark.parametrize("raw, kind, text", [
    ("Blocked: HTTP 403", "blocked", "被网站暂时限制，稍后再试"),
    ("HTTP 429", "blocked", "被网站暂时限制，稍后再试"),
    ("Blocked: Boss code=37 environment abnormal", "blocked", "被网站暂时限制，稍后再试"),
    ("all egress for nowcoder are cooling down", "blocked", "被网站暂时限制，稍后再试"),
    ("nowcoder is limiting requests right now; try again later", "blocked", "被网站暂时限制，稍后再试"),
    ("LoginRequired: HTTP 401", "login", "需要先登录"),
    ("login required", "login", "需要先登录"),
    ("nowcoder: login required; log in and try again", "login", "需要先登录"),
    ("StructureError: missing data.datas", "structure", "网站页面变了，暂时读不出来"),
    ("ParseError: non-JSON body: '<html>'", "structure", "网站页面变了，暂时读不出来"),
    ("nowcoder: could not load the detail (StructureError: shape)", "structure", "网站页面变了，暂时读不出来"),
    ("missing environment variable(s): DJS_YUPAO_NONCE_KEY", "env", "还没配置这个网站需要的密钥"),
    ("MissingEnv: environment variable DJS_X is not set", "env", "还没配置这个网站需要的密钥"),
    ("Playwright/Chromium missing. Run: pip install playwright && playwright install chromium (Error)", "browser", "需要先安装浏览器组件"),
    ("BrowserType.launch: Executable doesn't exist at /x/chromium", "browser", "需要先安装浏览器组件"),
    ("ConnectError: [Errno 61] Connection refused", "network", "网络不通，稍后再试"),
    ("ReadTimeout: timed out", "network", "网络不通，稍后再试"),
    ("nowcoder: could not load the detail (ConnectTimeout: )", "network", "网络不通，稍后再试"),
    ("login not completed within 180s", "timeout", "等了三分钟还没有登录成功，请再试一次"),
    ("BOSS 直聘暂不支持城市「火星」，可选：北京、上海等", "city", "这个网站暂时不支持这个城市"),
    ("58同城必须指定城市", "city", "这个网站需要先填写城市"),
    ("HTTPStatusError: Server error '500 Internal Server Error'", "other", "没成功，可以稍后重试"),
    ("", "other", "没成功，可以稍后重试"),
    (None, "other", "没成功，可以稍后重试"),
])
def test_friendly_error_table(raw, kind, text):
    assert results.classify(raw) == (kind, text) and results.friendly_error(raw) == text


def test_friendly_text_never_leaks_the_raw_error():
    assert "403" not in results.friendly_error("Blocked: HTTP 403 at https://secret.test/?token=abc")


# ---- annotate_searches / requeue ----
def test_annotate_searches_adds_reason_kind_and_the_newest_task_per_site(conn):
    old = task(conn, "nowcoder", "py", "杭州", status="dead")
    conn.execute("UPDATE tasks SET last_error='Blocked: HTTP 403' WHERE id=?", (old,))
    new = task(conn, "nowcoder", "py", "杭州", status="dead")
    conn.execute("UPDATE tasks SET last_error='missing environment variable(s): DJS_X' WHERE id=?", (new,))
    quiet = task(conn, "chinahr", "py", "杭州", status="dead")   # dead without any recorded error
    ok = task(conn, "iguopin", "py", "杭州")
    [search] = results.annotate_searches(conn, service.searches(conn))
    sites = {s["provider"]: s for s in search["per_site"]}
    assert (sites["nowcoder"]["task_id"], sites["nowcoder"]["kind"], sites["nowcoder"]["reason"]) == (new, "env", "还没配置这个网站需要的密钥")
    assert (sites["chinahr"]["task_id"], sites["chinahr"]["kind"]) == (quiet, "other")
    assert (sites["iguopin"]["task_id"], sites["iguopin"]["kind"], sites["iguopin"]["reason"]) == (ok, None, None)
    assert all("last_error" not in s for s in search["per_site"])


def test_requeue_restarts_dead_and_login_blocked_tasks_only(conn):
    dead, waiting, done, pending = (task(conn, "nowcoder", "py", status=s) for s in ("dead", "waiting_login", "done", "pending"))
    conn.execute("UPDATE tasks SET attempts=8, last_error='boom', next_run_at=9e9 WHERE id IN (?,?)", (dead, waiting))
    assert taskqueue.requeue(conn, [dead, waiting, done, pending, 999]) == 2
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM tasks")}
    for t in (dead, waiting):
        assert (rows[t]["status"], rows[t]["attempts"], rows[t]["last_error"], rows[t]["next_run_at"]) == ("pending", 0, None, 0)
    assert (rows[done]["status"], rows[pending]["status"]) == ("done", "pending")
    assert taskqueue.requeue(conn, []) == 0
    assert taskqueue.claim(conn)["id"] == dead   # and the queue picks them up again
    with pytest.raises(ValueError):
        taskqueue.requeue(conn, ["x"])
