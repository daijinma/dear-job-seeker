"""djs.query: the one read path for the results page and the MCP tool. Filters in SQL (pay per month, words, city,
platform, search, task, completeness), each job once with the best record kept, and the other platforms it is on."""
import pytest

from djs import db, query
from provider_fakes import job, task


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DJS_HOME", str(tmp_path))
    c = db.connect()
    yield c
    c.close()


def titles(wh, conn, **kw):
    rows, total = query.find(conn, wh, **{"dedupe": False, "limit": 100, **kw})
    assert total == len(rows)
    return [r["title"] for r in rows]


def where(**kw):
    return query.where(**kw)


# ---- filters
@pytest.fixture
def paid(conn):
    """One search with jobs paid per month, day, hour and year, and one without a salary."""
    t = task(conn, "nowcoder", "py", "杭州")
    pay = {"month15_25": (15000, 25000, "元/月"), "day600_800": (600, 800, "元/天"),     # 13050 - 17400 a month
           "month8_12": (8000, 12000, "元/月"), "year240_360": (240000, 360000, "元/年"),   # 20000 - 30000 a month
           "hour80_100": (80, 100, "元/时")}                                              # 13920 - 17400 a month
    for sid, (lo, hi, unit) in pay.items():
        job(conn, "nowcoder", sid, [t], title=sid, salary_min=lo, salary_max=hi, salary_unit=unit)
    job(conn, "nowcoder", "none", [t], title="none", complete=0, missing="salary")
    return t


def test_a_minimum_salary_keeps_the_jobs_that_can_pay_it_whatever_the_unit(conn, paid):
    got = titles(where(min_month_salary=15000, complete_only=False), conn)
    assert sorted(got) == ["day600_800", "hour80_100", "month15_25", "year240_360"]   # no figures, no match
    assert sorted(titles(where(min_month_salary=17401, complete_only=False), conn)) == ["month15_25", "year240_360"]
    assert titles(where(min_month_salary=30001), conn) == []


def test_a_maximum_salary_keeps_the_jobs_that_start_at_or_below_it(conn, paid):
    assert sorted(titles(where(max_month_salary=13050), conn)) == ["day600_800", "month8_12"]
    assert sorted(titles(where(max_month_salary=8000), conn)) == ["month8_12"]


def test_both_bounds_ask_for_a_range_that_overlaps_the_jobs_range(conn, paid):
    assert sorted(titles(where(min_month_salary=16000, max_month_salary=18000), conn)) == [
        "day600_800", "hour80_100", "month15_25"]   # the year job starts at 20000: above the asked range
    assert titles(where(min_month_salary=9000, max_month_salary=9500), conn) == ["month8_12"]


def test_a_job_without_a_salary_matches_no_salary_filter_but_is_listed_without_one(conn, paid):
    assert "none" not in titles(where(min_month_salary=0, complete_only=False), conn)
    assert "none" in titles(where(complete_only=False), conn)


def test_text_needs_every_word_in_the_title_or_the_description_in_any_case(conn):
    t = task(conn, "nowcoder", "x")
    job(conn, "nowcoder", "a", [t], title="Python 后端工程师", description="使用 Django 和 Redis")
    job(conn, "nowcoder", "b", [t], title="Java 后端", description="使用 Spring 和 redis")
    job(conn, "nowcoder", "c", [t], title="Python 数据", description=None)
    job(conn, "nowcoder", "d", [t], title="产品经理", description="100% 负责; a_b")
    got = lambda text: sorted(titles(where(text=text), conn))   # noqa: E731
    assert got("python django") == ["Python 后端工程师"]                # one word in the title, one in the description
    assert got("PYTHON  后端") == ["Python 后端工程师"]                 # any case, any amount of space
    assert got("redis") == ["Java 后端", "Python 后端工程师"]           # the description alone is enough
    assert got("python spring") == []                                   # every word must be there
    assert got("python") == ["Python 后端工程师", "Python 数据"]        # a job without a description can match on its title
    assert got("100%") == ["产品经理"] and got("a_b") == ["产品经理"]   # % and _ are only themselves
    assert got("%") == ["产品经理"] and got("_") == ["产品经理"]
    assert got("") == got(None) == ["Java 后端", "Python 后端工程师", "Python 数据", "产品经理"]
    with pytest.raises(ValueError, match="at most"):
        where(text=" ".join("w" * 11))


def test_city_filter_matches_the_jobs_place_and_ignores_a_shi_suffix(conn):
    t = task(conn, "nowcoder", "x")
    for sid, city in (("a", "杭州"), ("b", "杭州·滨江区"), ("c", "上海、杭州"), ("d", "上海"), ("e", None)):
        job(conn, "nowcoder", sid, [t], title=sid, city=city)
    assert sorted(titles(where(city="杭州市"), conn)) == ["a", "b", "c"]
    assert sorted(titles(where(city="上海"), conn)) == ["c", "d"]
    assert sorted(titles(where(city=""), conn)) == ["a", "b", "c", "d", "e"]


def test_scopes_search_task_and_platform_combine(conn):
    hz, sh = task(conn, "nowcoder", "py", "杭州"), task(conn, "chinahr", "py", "杭州")
    other = task(conn, "nowcoder", "py", "上海")
    job(conn, "nowcoder", "a", [hz, other], title="a")
    job(conn, "chinahr", "b", [sh], title="b")
    job(conn, "nowcoder", "c", [other], title="c")
    assert sorted(titles(where(search=("py", "杭州")), conn)) == ["a", "b"]
    assert sorted(titles(where(search=("py", "上海")), conn)) == ["a", "c"]   # a job a repeat search saw again is in both
    assert titles(where(search=("py", "杭州"), provider="chinahr"), conn) == ["b"]
    assert titles(where(task_id=sh), conn) == ["b"]
    assert titles(where(task_id=hz, provider="chinahr"), conn) == []
    assert sorted(titles(where(), conn)) == ["a", "b", "c"]
    assert titles(where(search=("py", None), provider="nowcoder"), conn) == []   # nobody searched without a city


def test_complete_only_is_the_default_and_the_filters_stack(conn):
    t = task(conn, "nowcoder", "x")
    job(conn, "nowcoder", "ok", [t], title="ok", city="杭州", salary_min=1, salary_max=1, salary_unit="元/月",
        description="django")
    job(conn, "nowcoder", "bad", [t], title="bad", city="杭州", complete=0, missing="description",
        salary_min=1, salary_max=1, salary_unit="元/月")
    assert titles(where(), conn) == ["ok"]
    assert sorted(titles(where(complete_only=False), conn)) == ["bad", "ok"]
    assert titles(where(city="杭州", min_month_salary=1, text="django", provider="nowcoder", task_id=t), conn) == ["ok"]


# ---- each job once
def posting(conn, t, provider, sid, seen, **cols):
    """One record of the job '甲公司 / Python 后端 / 杭州' on a platform."""
    cols = {"title": "Python 后端", "company": "甲公司科技有限公司", "city": "杭州", **cols}
    return job(conn, provider, sid, [t], seen=seen, **cols)


def test_one_job_on_several_platforms_is_listed_once_as_its_best_record(conn):
    t = task(conn, "nowcoder", "py", "杭州")
    thin = posting(conn, t, "nowcoder", "n1", 100)
    rich = posting(conn, t, "chinahr", "c1", 100, district="西湖区", education="本科", experience="3年")
    posting(conn, t, "iguopin", "g1", 300, title="Other job")   # not a duplicate of the three above
    rows, total = query.find(conn, where(search=("py", "杭州")))
    assert total == 2 and sorted(r["title"] for r in rows) == ["Other job", "Python 后端"]
    best = next(r for r in rows if r["title"] == "Python 后端")
    assert best["id"] == rich and best["provider"] == "chinahr"   # more columns filled
    assert [(o["provider"], o["label"], o["id"], o["source_url"]) for o in best["also_on"]] == [
        ("nowcoder", "牛客网", thin, "https://x.test/n1")]
    assert next(r for r in rows if r["title"] == "Other job")["also_on"] == []


def test_the_best_record_is_complete_first_then_more_filled_then_newest(conn):
    t = task(conn, "nowcoder", "py", "杭州")
    unfinished = posting(conn, t, "chinahr", "c", 900, complete=0, missing="salary", district="x", education="y",
                         experience="z", description="d")                       # the most filled, and the newest, but incomplete
    plain = posting(conn, t, "nowcoder", "n", 100)
    fuller = posting(conn, t, "iguopin", "g", 100, district="西湖区")
    later = posting(conn, t, "zhaopin", "z", 200, district="余杭区")             # as filled as fuller, and newer
    kept = lambda **kw: query.find(conn, where(search=("py", "杭州"), **kw), sort="newest")[0]   # noqa: E731
    assert [r["id"] for r in kept()] == [later]                                  # incomplete ones are not listed at all
    assert [r["id"] for r in kept(complete_only=False)] == [later]               # complete beats the fullest incomplete record
    assert sorted(o["id"] for o in kept(complete_only=False)[0]["also_on"]) == sorted([unfinished, plain, fuller])
    assert [r["id"] for r in kept(provider="nowcoder")] == [plain]               # the filters decide who is in the running
    assert [r["id"] for r in kept(provider="chinahr", complete_only=False)] == [unfinished]
    assert kept(provider="nowcoder")[0]["also_on"] == []                          # the others are outside this view: nothing is folded


def test_postings_of_one_platform_are_never_merged_with_each_other(conn):
    """One company posting the same title twice on one site: other level, other pay, other JD. Two postings, two jobs."""
    t = task(conn, "nowcoder", "py", "杭州")
    senior = posting(conn, t, "nowcoder", "n1", 100, salary_min=25000, salary_max=40000, salary_unit="元/月")
    junior = posting(conn, t, "nowcoder", "n2", 200, salary_min=15000, salary_max=25000, salary_unit="元/月")
    rows, total = query.find(conn, where(search=("py", "杭州")))
    assert total == 2 and sorted(r["id"] for r in rows) == [senior, junior]
    assert all(r["also_on"] == [] for r in rows)
    assert query.count_by_provider(conn, where(search=("py", "杭州"))) == {"nowcoder": 2}
    assert query.count(conn, where(search=("py", "杭州")), dedupe=True) == 2


def test_a_posting_on_another_platform_is_folded_into_the_matching_posting_only(conn):
    """The best posting of each platform stands for the job on every platform; the platform's other postings stay."""
    t = task(conn, "nowcoder", "py", "杭州")
    best_n = posting(conn, t, "nowcoder", "n1", 200, district="西湖区")        # nowcoder's best
    other_n = posting(conn, t, "nowcoder", "n2", 100)                             # its second posting
    on_chinahr = posting(conn, t, "chinahr", "c1", 150)
    rows, total = query.find(conn, where(search=("py", "杭州")), sort="newest")
    assert total == 2 and [r["id"] for r in rows] == [best_n, other_n]            # chinahr's is folded: it has nothing of its own to show
    assert [o["id"] for o in rows[0]["also_on"]] == [on_chinahr] and rows[1]["also_on"] == []
    assert query.count_by_provider(conn, where(search=("py", "杭州"))) == {"nowcoder": 2, "chinahr": 1}   # every platform keeps its own page
    shown = query.find(conn, where(search=("py", "杭州"), provider="chinahr"))[0]
    assert [r["id"] for r in shown] == [on_chinahr] and shown[0]["also_on"] == []   # ... and there it is, whole
    assert query.count(conn, where(search=("py", "杭州")), dedupe=False) == 3


def test_every_other_platform_has_its_record_folded_in_once(conn):
    t = task(conn, "nowcoder", "py", "杭州")
    for provider, sid, seen in (("nowcoder", "n", 300), ("chinahr", "c", 200), ("iguopin", "g", 100), ("zhaopin", "z", 50)):
        posting(conn, t, provider, sid, seen)
    rows, total = query.find(conn, where(search=("py", "杭州")))
    assert total == 1 and rows[0]["provider"] == "nowcoder"
    assert [o["provider"] for o in rows[0]["also_on"]] == ["chinahr", "iguopin", "zhaopin"]   # best first: the newest of equals


def test_a_job_without_a_fingerprint_is_never_merged(conn):
    t = task(conn, "nowcoder", "py")
    job(conn, "nowcoder", "a", [t], title="same", company=None, city="杭州")
    job(conn, "chinahr", "b", [t], title="same", company=None, city="杭州")
    rows, total = query.find(conn, where())
    assert total == 2 and all(r["also_on"] == [] for r in rows)


def test_dedupe_off_lists_every_record_and_names_no_other_platform(conn):
    t = task(conn, "nowcoder", "py", "杭州")
    for provider, sid in (("nowcoder", "n"), ("chinahr", "c"), ("iguopin", "g")):
        posting(conn, t, provider, sid, 100)
    rows, total = query.find(conn, where(), dedupe=False)
    assert total == 3 and len(rows) == 3 and all(r["also_on"] == [] for r in rows)
    assert query.find(conn, where(), dedupe=True)[1] == 1
    assert (query.count(conn, where(), True), query.count(conn, where(), False)) == (1, 3)


def test_paging_counts_each_job_once_and_never_repeats_or_skips(conn):
    t = task(conn, "nowcoder", "py", "杭州")
    for i in range(7):
        posting(conn, t, "nowcoder", f"n{i}", 100 + i, title=f"job {i}")
        posting(conn, t, "chinahr", f"c{i}", 200 + i, title=f"job {i}", district="x")   # each is on both platforms
    pages = [query.find(conn, where(), limit=3, offset=o, sort="newest") for o in (0, 3, 6, 9)]
    assert [len(rows) for rows, _ in pages] == [3, 3, 1, 0] and {total for _, total in pages} == {7}
    ids = [r["id"] for rows, _ in pages for r in rows]
    assert len(set(ids)) == 7


def test_sorts_newest_mixed_salary_and_company(conn):
    t = task(conn, "nowcoder", "x")
    job(conn, "nowcoder", "a", [t], title="a", seen=100, company="乙", salary_min=1, salary_max=2, salary_unit="元/时")
    job(conn, "chinahr", "b", [t], title="b", seen=300, company="甲", salary_min=9000, salary_max=9000, salary_unit="元/月")
    job(conn, "nowcoder", "c", [t], title="c", seen=200, company=None)
    order = lambda sort: [r["title"] for r in query.find(conn, where(), sort=sort, dedupe=False)[0]]   # noqa: E731
    assert order("newest") == ["b", "c", "a"]
    assert order("mixed") == ["c", "b", "a"]      # a turn each (nowcoder's newest, chinahr's), then nowcoder's next
    assert order("salary") == ["b", "a", "c"]     # 9000 a month, 348 a month, no salary last
    assert order("company") == ["a", "b", "c"]    # 乙 < 甲 as characters, unknown last
    with pytest.raises(KeyError):
        order("bogus")


# ---- what is still to be completed
def due(conn, job_pk):
    return conn.execute(f"SELECT {query.DUE} FROM jobs j WHERE j.id=?", (job_pk,)).fetchone()["detail_due"]


def test_a_job_is_due_for_its_detail_only_while_a_task_that_asks_for_details_is_queued_or_running(conn):
    running = task(conn, "nowcoder", "py", status="running")
    queued = task(conn, "nowcoder", "py", status="pending")
    finished = task(conn, "nowcoder", "py", status="done")
    never = task(conn, "nowcoder", "py", status="running")
    conn.execute("UPDATE tasks SET payload=json_set(payload, '$.detail', 'never') WHERE id=?", (never,))
    old_never = task(conn, "nowcoder", "py", status="running")
    conn.execute("UPDATE tasks SET payload=json_set(payload, '$.detail', json('false')) WHERE id=?", (old_never,))
    always = task(conn, "nowcoder", "py", status="pending")
    conn.execute("UPDATE tasks SET payload=json_set(payload, '$.detail', 'always') WHERE id=?", (always,))
    assert [due(conn, job(conn, "nowcoder", f"j{t}", [t])) for t in (running, queued, always)] == [1, 1, 1]
    assert [due(conn, job(conn, "nowcoder", f"k{t}", [t])) for t in (finished, never, old_never)] == [0, 0, 0]
    both = job(conn, "nowcoder", "both", [finished, running])
    assert due(conn, both) == 1   # one running task is enough
    has_detail = job(conn, "nowcoder", "d", [running], detail="{}")
    failed = job(conn, "nowcoder", "e", [running], detail_error="boom")
    assert (due(conn, has_detail), due(conn, failed)) == (0, 0)   # asked already, whatever came of it
    conn.execute("UPDATE tasks SET status='done'")
    assert due(conn, both) == 0
