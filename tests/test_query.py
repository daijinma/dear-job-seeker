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


def pay_range(conn):
    """The 'at least 8000 a month' case: three jobs that differ only in how their pay range sits around 8000."""
    t = task(conn, "nowcoder", "会计", "宁波")
    for sid, (lo, hi) in {"6k-9k": (6000, 9000), "8k-12k": (8000, 12000), "9k-15k": (9000, 15000), "3k-5k": (3000, 5000)}.items():
        job(conn, "nowcoder", sid, [t], title=sid, salary_min=lo, salary_max=hi, salary_unit="元/月")
    job(conn, "nowcoder", "none", [t], title="none", complete=0, missing="salary")


def test_a_floor_keeps_the_jobs_whose_lowest_pay_is_at_least_it_and_a_minimum_the_jobs_whose_range_reaches_it(conn):
    pay_range(conn)
    ask = lambda **kw: sorted(titles(where(**kw), conn))   # noqa: E731
    assert ask(floor_month_salary=8000) == ["8k-12k", "9k-15k"]                    # "8000 以上": 6k-9k starts below it
    assert ask(min_month_salary=8000) == ["6k-9k", "8k-12k", "9k-15k"]             # 6k-9k reaches 8000: it is in
    assert ask(floor_month_salary=8001) == ["9k-15k"] and ask(floor_month_salary=9000) == ["9k-15k"]   # at least: 9000 itself counts
    assert ask(floor_month_salary=9001) == [] and ask(floor_month_salary=0) == ["3k-5k", "6k-9k", "8k-12k", "9k-15k"]
    assert ask(floor_month_salary=3000, max_month_salary=8000) == ["3k-5k", "6k-9k", "8k-12k"]   # a floor and a ceiling on the start
    assert ask(floor_month_salary=6000, min_month_salary=12500) == ["9k-15k"]                   # the two stack
    assert "none" not in titles(where(floor_month_salary=0, complete_only=False), conn)         # a salary filter needs a salary


def test_a_floor_works_on_the_pay_per_month_whatever_the_unit(conn, paid):
    assert sorted(titles(where(floor_month_salary=13050), conn)) == ["day600_800", "hour80_100", "month15_25", "year240_360"]
    assert sorted(titles(where(floor_month_salary=13051), conn)) == ["hour80_100", "month15_25", "year240_360"]   # a day is 21.75 days
    assert sorted(titles(where(floor_month_salary=15000), conn)) == ["month15_25", "year240_360"]


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


def test_text_also_reads_the_welfare_tags_and_title_does_not(conn):
    t = task(conn, "job51", "会计", "宁波")
    job(conn, "job51", "a", [t], title="会计", description="负责做账", benefits="五险一金、双休、餐饮补贴")        # 双休 only in its tags
    job(conn, "job51", "b", [t], title="会计助理", description="每周双休，做账", benefits=None)                  # in the text only
    job(conn, "job51", "c", [t], title="会计主管", description="管理团队", benefits="五险一金、Python培训")
    job(conn, "job51", "d", [t], title="出纳", description=None, benefits=None)
    got = lambda **kw: sorted(titles(where(**kw), conn))   # noqa: E731
    assert got(text="双休") == ["会计", "会计助理"]                       # the tags are searched next to title and description
    assert got(text="五险一金 双休") == ["会计"]                           # every word, each in whichever of the three it sits
    assert got(text="做账 餐饮补贴") == ["会计"]                           # a word in the description and one in the tags
    assert got(text="PYTHON培训") == ["会计主管"] and got(text="python") == ["会计主管"]   # any case
    assert got(title="双休") == [] and got(title="会计", text="双休") == ["会计", "会计助理"]   # the role is searched in the title alone
    assert got(text="%") == [] and got(text="_") == []                    # wildcards are only themselves
    assert got(text="") == got(text=None) == sorted(["出纳", "会计", "会计助理", "会计主管"])


def test_title_needs_every_word_in_the_title_alone_in_any_case(conn):
    t = task(conn, "nowcoder", "x")
    job(conn, "nowcoder", "a", [t], title="Python 后端工程师", description="使用 Django 和 Redis")
    job(conn, "nowcoder", "b", [t], title="Java 后端", description="熟悉 python，使用 Django")   # python only in the description
    job(conn, "nowcoder", "c", [t], title="Python 数据", description=None)
    got = lambda **kw: sorted(titles(where(**kw), conn))   # noqa: E731
    assert got(title="python") == ["Python 后端工程师", "Python 数据"]                      # the description of "Java 后端" does not count
    assert got(text="python") == ["Java 后端", "Python 后端工程师", "Python 数据"]           # text reads the description too
    assert got(title="PYTHON  后端") == ["Python 后端工程师"]                               # every word, any case, any amount of space
    assert got(title="django") == [] and got(text="django") == ["Java 后端", "Python 后端工程师"]   # a skill that sits in descriptions only
    assert got(title="python", text="django") == ["Python 后端工程师"]                      # the role in the title, the skill anywhere
    assert got(title="") == got(title=None) == got() == ["Java 后端", "Python 后端工程师", "Python 数据"]   # no words: no filter
    for field in ("title", "text"):
        with pytest.raises(ValueError, match=f"{field} takes at most"):
            where(**{field: " ".join("w" * 11)})


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


# ---- the same job on the other platforms, for one job
def test_also_on_names_the_best_record_of_each_other_platform_and_nothing_without_a_fingerprint(conn):
    t = task(conn, "nowcoder", "py", "杭州")
    mine = posting(conn, t, "nowcoder", "n1", 100)
    thin = posting(conn, t, "chinahr", "c1", 300)
    rich = posting(conn, t, "chinahr", "c2", 100, district="西湖区", education="本科")      # chinahr's better record of the job
    unfinished = posting(conn, t, "iguopin", "g1", 900, complete=0, missing="salary", district="x", education="y")
    elsewhere = posting(conn, t, "zhaopin", "z1", 100, title="Other job")
    nameless = job(conn, "nowcoder", "n2", [t], title="same", company=None, city="杭州")
    job(conn, "chinahr", "c3", [t], title="same", company=None, city="杭州")
    assert [(o["provider"], o["label"], o["id"], o["source_url"]) for o in query.also_on(conn, mine)] == [
        ("chinahr", "新华英才", rich, "https://x.test/c2"), ("iguopin", "国聘", unfinished, "https://x.test/g1")]
    assert thin and elsewhere and [o["provider"] for o in query.also_on(conn, rich)] == ["nowcoder", "iguopin"]
    assert query.also_on(conn, nameless) == [] and query.also_on(conn, 999) == []     # no fingerprint: never matched on a guess


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


def relevant_search(conn):
    """The search 'Python 后端' on two platforms; per platform the newest job is the one that holds none of its words."""
    n, c = task(conn, "nowcoder", "Python 后端"), task(conn, "chinahr", "Python 后端")
    for sid, seen, title in (("n_new", 300, "风电结构仿真分析工程师"), ("n_two", 200, "Python后端开发工程师"), ("n_one", 100, "后端开发")):
        job(conn, "nowcoder", sid, [n], seen=seen, title=title)
    for sid, seen, title in (("c_new", 250, "软件工程师"), ("c_two", 150, "后端 PYTHON"), ("c_one", 50, "python 数据分析")):
        job(conn, "chinahr", sid, [c], seen=seen, title=title)
    return {r["source_job_id"]: r["id"] for r in conn.execute("SELECT id, source_job_id FROM jobs")}


def test_mixed_puts_the_titles_holding_more_of_the_keywords_words_first_then_the_sites_take_turns(conn):
    ids = relevant_search(conn)
    order = lambda keyword, sort="mixed": [r["id"] for r in query.find(conn, where(), sort=sort, keyword=keyword, limit=100)[0]]   # noqa: E731
    by_words = [ids[k] for k in ("n_two", "c_two", "n_one", "c_one", "n_new", "c_new")]   # both words | one word | none; the sites in turn
    plain = [ids[k] for k in ("n_new", "c_new", "n_two", "c_two", "n_one", "c_one")]      # the turns alone: each site's newest first
    assert order("Python 后端") == by_words
    assert order("  PYTHON   后端 ") == order("后端 python") == order("Python Python 后端") == by_words   # any case, space, order
    assert order(None) == order("") == order("Java") == plain          # no word in any title: nothing to rank by
    assert order("Python 后端", "newest") == plain                     # only 'mixed' ranks
    assert query.find(conn, where(), sort="mixed", keyword="Python 后端", limit=2, offset=2)[0][0]["id"] == ids["n_one"]   # pages continue


def test_a_word_counts_when_the_title_holds_it_anywhere_and_wildcards_are_only_themselves(conn):
    t = task(conn, "nowcoder", "前端")
    job(conn, "nowcoder", "a", [t], seen=200, title="产品经理")
    job(conn, "nowcoder", "b", [t], seen=100, title="资深前端开发（含 100% 远程）")
    assert titles(where(), conn, sort="mixed", keyword="前端 100%") == ["资深前端开发（含 100% 远程）", "产品经理"]   # % is only itself
    assert titles(where(), conn, sort="mixed", keyword="前端 _ %") == ["资深前端开发（含 100% 远程）", "产品经理"]


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
