"""djs.normalize: salary text and figures, plain-text JD, welfare tags, the list of missing core elements. Pure functions,
no I/O."""
import pytest

from djs.normalize import CORE, UNITS, benefits, fingerprint, missing, monthly, parse_salary, plain_text, salary_range

M, D, H, Y = UNITS   # 元/月 元/天 元/时 元/年

# (text as a site words it, (min, max, unit, paid months)). Every shape below was seen on a live platform.
SALARIES = [
    ("15-25K·14薪", (15000, 25000, M, 14)),                  # boss
    ("20-30K", (20000, 30000, M, None)),
    ("5-8K·13薪", (5000, 8000, M, 13)),
    ("200-300元/天", (200, 300, D, None)),
    ("1.4-1.7万", (14000, 17000, M, None)),                  # zhaopin
    ("1.4-2.3万·14薪", (14000, 23000, M, 14)),
    ("1.5-3万·16薪", (15000, 30000, M, 16)),
    ("6000-8000元", (6000, 8000, M, None)),
    ("20K-40K", (20000, 40000, M, None)),                    # a unit on both sides
    ("100元/时", (100, 100, H, None)),
    ("5k-8k元/月", (5000, 8000, M, None)),                   # chinahr
    ("7.5k-12.5k元/月", (7500, 12500, M, None)),
    ("8千-1.3万", (8000, 13000, M, None)),                   # ciic: each side keeps its own unit
    ("9千 - 1.4万", (9000, 14000, M, None)),
    ("6千-8千", (6000, 8000, M, None)),
    ("3000-4000元/月", (3000, 4000, M, None)),               # yupao
    ("1.8-3.5万元/月", (18000, 35000, M, None)),             # the unit on one side only, then 元/月 after it
    ("1-2万元/月", (10000, 20000, M, None)),
    ("300-400元/天", (300, 400, D, None)),
    ("13-16万/年", (130000, 160000, Y, None)),               # job51
    ("20-30万/年", (200000, 300000, Y, None)),
    ("6-8千", (6000, 8000, M, None)),
    ("3-3.5千", (3000, 3500, M, None)),
    ("1.2-2.4万·15薪", (12000, 24000, M, 15)),
    ("10000-13000元", (10000, 13000, M, None)),              # ganji
    ("8000-15000元", (8000, 15000, M, None)),
    ("15000-25000元/月", (15000, 25000, M, None)),           # job58
    ("8000-12000元/月·14薪", (8000, 12000, M, 14)),
    ("10,000-15,000元/月", (10000, 15000, M, None)),         # thousands separators
    ("1.5－2万", (15000, 20000, M, None)),                    # full-width hyphen
    ("4000元/月", (4000, 4000, M, None)),                    # one figure is a range of one
    ("5万", (50000, 50000, M, None)),
    # spelling variants
    ("15-25k·14薪", (15000, 25000, M, 14)),
    ("15-25K×14薪", (15000, 25000, M, 14)),
    ("15-25K x 14薪", (15000, 25000, M, 14)),
    ("20-30K 13薪", (20000, 30000, M, 13)),
    ("10~15K", (10000, 15000, M, None)),
    ("10至15K", (10000, 15000, M, None)),
    ("年薪20-30万", (200000, 300000, Y, None)),
    ("日薪200-300元", (200, 300, D, None)),
    ("时薪50-60元", (50, 60, H, None)),
    ("月薪:1.2万-1.5万", (12000, 15000, M, None)),
    ("50-60元/小时", (50, 60, H, None)),
]

# Nothing safe to take: a negotiable job, a one-sided bound, a sentinel, a unit we have no column for, plain junk.
NOT_SALARIES = ["面议", "薪资面议", "", "   ", "无", "abc", "junk-data-xyz", "6000", "5-8", "0-9999999", "0-5000", "300-100元",
                "1万以上", "3000+", "1千以下-1千以下", "3000元起", "500元/周", "5元/件", "面议 8000-9000元", None, 12000, ["8-9K"]]


@pytest.mark.parametrize("text,want", SALARIES, ids=[t for t, _ in SALARIES])
def test_parse_salary(text, want):
    assert parse_salary(text) == dict(zip(("salary_min", "salary_max", "salary_unit", "salary_months"), want))


@pytest.mark.parametrize("text", NOT_SALARIES, ids=[repr(t) for t in NOT_SALARIES])
def test_parse_salary_never_guesses(text):
    assert parse_salary(text) is None


def test_parse_salary_is_total_and_months_are_never_multiplied():
    assert len(SALARIES) >= 30 and len(NOT_SALARIES) >= 10
    got = parse_salary("20-30K·15薪")
    assert (got["salary_min"], got["salary_max"], got["salary_months"]) == (20000, 30000, 15)   # not 15 x 20000
    assert parse_salary("20-30K·6薪")["salary_months"] is None   # not a plausible pay plan


@pytest.mark.parametrize("args,want", [
    ((8000, 15000), {"salary_min": 8000, "salary_max": 15000, "salary_unit": M, "salary_months": None}),
    # only a monthly salary has paid months
    (("8000", "15000.4", D, 14), {"salary_min": 8000, "salary_max": 15000, "salary_unit": D, "salary_months": None}),
    ((8000, 15000, M, 14), {"salary_min": 8000, "salary_max": 15000, "salary_unit": M, "salary_months": 14}),
    ((8000, 15000, M, 12), {"salary_min": 8000, "salary_max": 15000, "salary_unit": M, "salary_months": 12}),
    ((8000, 8000), {"salary_min": 8000, "salary_max": 8000, "salary_unit": M, "salary_months": None}),
    ((0, 9999999), {}), ((0, 5000), {}), ((9000, 8000), {}), ((100, 200, "元/周"), {}), ((None, 5000), {}),
    (("", ""), {}), (("x", 5), {}), ((float("inf"), float("inf")), {}), ((float("nan"), 5), {}),
])
def test_salary_range(args, want):
    assert salary_range(*args) == want


def test_salary_range_months_must_be_a_plausible_number_of_payments():
    for months in (None, 0, 1, 11, 37, True, "14", 14.5):
        assert salary_range(1, 2, M, months)["salary_months"] is None
    for months in (12, 13, 14, 16, 24, 36):
        assert salary_range(1, 2, M, months)["salary_months"] == months


# ---- plain_text
@pytest.mark.parametrize("raw,want", [
    ("<p>负责造价</p>", "负责造价"),
    ("<p>1，负责市场开拓… </p>\n<p>2，完成业绩目标…</p>", "1，负责市场开拓…\n\n2，完成业绩目标…"),
    ("【岗位职责】<br/>-负责日常清洁<br>-保持整洁<br /><br/>【职位要求】", "【岗位职责】\n-负责日常清洁\n-保持整洁\n\n【职位要求】"),
    ("<ul><li>甲</li><li>乙</li></ul>", "甲\n乙"),
    ("<div>x</div><div>y</div>", "x\ny"),
    ("<span style='color:#0099ff'>python</span>开发", "python开发"),
    ("A&amp;B&nbsp;C", "A&B C"),
    ("&lt;b&gt;不是标签&lt;/b&gt;", "<b>不是标签</b>"),            # entities are decoded after the tags are gone
    ("5<10 and 7>3", "5<10 and 7>3"),
    ("<script>alert(1)</script>正文<style>p{color:red}</style>", "正文"),
    ("<SCRIPT type='text/javascript'>\nvar a = '</p>';\n</SCRIPT>正文", "正文"),
    ("a<br><br><br><br>b", "a\n\nb"),
    ("行尾空格   \n下一行\t\n", "行尾空格\n下一行"),
    ("第一行\r\n第二行\r第三行", "第一行\n第二行\n第三行"),
    ("###标题<br/><br/>正文", "###标题\n\n正文"),
    ("纯文本\n不变", "纯文本\n不变"),
    ("   ", ""), ("", ""), (None, ""),
])
def test_plain_text(raw, want):
    assert plain_text(raw) == want


# ---- missing
FULL = {"title": "后端", "company": "甲", "city": "杭州", "description": "做事", "salary_min": 1, "salary_max": 2}


def test_missing_lists_absent_core_elements_in_contract_order():
    assert missing(FULL) == []
    assert missing({}) == list(CORE) == ["title", "company", "city", "salary", "description"]
    assert missing({**FULL, "company": None, "description": "  \n"}) == ["company", "description"]
    assert missing({**FULL, "title": "", "city": " "}) == ["title", "city"]


def test_a_salary_counts_only_as_two_numbers():
    assert missing({**FULL, "salary_max": None}) == ["salary"]
    assert missing({**FULL, "salary_min": None}) == ["salary"]
    assert missing({**FULL, "salary_min": "1", "salary_max": "2"}) == ["salary"]    # text is not a figure
    assert missing({**{k: v for k, v in FULL.items() if k != "salary_min"}, "salary_text": "面议"}) == ["salary"]


# ---- the salary per month
def pay(lo, hi, unit):
    return {"salary_min": lo, "salary_max": hi, "salary_unit": unit}


@pytest.mark.parametrize("fields, want", [
    (pay(15000, 25000, M), (15000, 25000)),     # a month is a month
    (pay(200, 400, D), (4350, 8700)),           # a day is 21.75 working days
    (pay(50, 100, H), (8700, 17400)),           # an hour is 174 hours
    (pay(200000, 480000, Y), (16667, 40000)),   # a year is a twelfth of its pay a month
    (pay(4000, 4000, M), (4000, 4000)),         # a range of one
    (pay(1, 1, H), (174, 174)),
    ({**pay(8000, 12000, M), "salary_months": 14}, (8000, 12000)),   # the paid months are not part of the monthly figure
])
def test_monthly_converts_every_unit_to_yuan_per_month(fields, want):
    assert tuple(monthly(fields).values()) == want
    assert list(monthly(fields)) == ["salary_month_min", "salary_month_max"]


@pytest.mark.parametrize("fields", [
    {}, {"salary_unit": M}, {"salary_min": 1, "salary_max": 2}, pay(1, None, M), pay(None, 2, M), pay("1", "2", M),
    pay(1.5, 2.5, M), pay(1, 2, "元/周"), pay(1, 2, None)])
def test_monthly_is_empty_without_a_whole_number_range_in_a_known_unit(fields):
    assert monthly(fields) == {"salary_month_min": None, "salary_month_max": None}


def test_every_unit_of_the_contract_converts():
    for unit in UNITS:   # a unit added to the contract must not silently lose its monthly figure
        assert monthly(pay(100, 100, unit))["salary_month_min"] is not None


# ---- the fingerprint that marks one job on several sites
def fp(company, title, city):
    return fingerprint({"company": company, "title": title, "city": city})


SAME_JOB = [   # every spelling in a group is one job
    [("某某科技有限公司", "Python后端开发工程师", "杭州"),
     ("某某科技（杭州）有限公司", "【14-16薪】Python 后端开发工程师", "杭州市"),   # a bracketed city, a tag, a space, a 市
     ("某某科技股份有限公司", "PYTHON 后端开发工程师 [急聘]", "杭州·滨江区"),     # legal form, case, tag, district
     ("某某集团", "python后端开发工程师", "杭州、上海"),                         # the first place of several
     ("某某 科技 有限公司", "Python-后端，开发工程师", "杭州 ")],                 # spaces and punctuation
    [("阿里巴巴（中国）有限公司", "Java开发", "杭州"), ("阿里巴巴(中国)有限公司", "Java 开发", "杭州"),
     ("阿里巴巴集团", "java开发", "杭州-西湖区")],
    [("ＡＢＣ科技股份有限公司", "Ｃ++开发", "上海"), ("abc科技", "c++开发", "上海市")],   # full-width letters
]
DIFFERENT_JOBS = [
    (("某某科技有限公司", "Python后端", "杭州"), ("某某科技有限公司", "Python前端", "杭州")),   # another job
    (("某某科技有限公司", "Python后端", "杭州"), ("某某科技有限公司", "Python后端", "上海")),   # another city
    (("某某科技有限公司", "Python后端", "杭州"), ("某某网络有限公司", "Python后端", "杭州")),   # another company
    (("某某", "C++开发", "杭州"), ("某某", "C#开发", "杭州")),                                  # the signs of C++ and C# stay
    (("某某", "Java开发(后端)", "杭州"), ("某某", "Java开发(前端)", "杭州")),                   # a round bracket is not a tag
]
NOT_ENOUGH = [(None, "x", "杭州"), ("科技有限公司", "x", "杭州"), ("某某", "", "杭州"), ("某某", "x", None),
              ("某某", "【急聘】", "杭州"), ("某某", "x", " · "), ("某某", None, None)]


@pytest.mark.parametrize("group", SAME_JOB)
def test_one_job_posted_in_different_spellings_has_one_fingerprint(group):
    assert len({fp(*spelling) for spelling in group}) == 1 and fp(*group[0]) is not None


@pytest.mark.parametrize("one, other", DIFFERENT_JOBS)
def test_different_jobs_keep_different_fingerprints(one, other):
    assert fp(*one) != fp(*other)


@pytest.mark.parametrize("company, title, city", NOT_ENOUGH)
def test_no_fingerprint_unless_company_title_and_city_are_all_known(company, title, city):
    assert fp(company, title, city) is None


def test_the_fingerprint_reads_like_its_parts():
    assert fp("杭州某某科技有限公司", "【14-16薪】Python 后端开发工程师", "杭州市") == "杭州某某|python后端开发工程师|杭州"


# ---- the welfare tags
@pytest.mark.parametrize("tags, want", [
    (["五险一金", "周末双休"], "五险一金、周末双休"),                   # the site's order
    (["五险一金", "双休", "五险一金", "双休", "餐补"], "五险一金、双休、餐补"),   # each once
    ([" 五险一金 ", "\u3000双休\n", "", "  ", None, 3, ["x"], {"a": 1}], "五险一金、双休"),   # blanks and what is no tag
    (("包吃", "包住"), "包吃、包住"),
    (["六险一金"], "六险一金"),
    ("五险一金", "五险一金"),                                           # one name given as a text
    ([], None), (None, None), ("", None), ("  ", None), ([None, ""], None),
    (3, None), ({"五险一金": 1}, None), (b"x", None),                   # nothing readable: none, never an error
])
def test_benefits_joins_the_tags_each_once_in_the_sites_order(tags, want):
    assert benefits(tags) == want


def test_benefits_is_no_core_element():
    assert "benefits" not in CORE
    assert missing({"title": "t", "company": "c", "city": "x", "salary_min": 1, "salary_max": 2, "description": "d"}) == []
