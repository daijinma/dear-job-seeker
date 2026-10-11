"""djs.cities (names, GB/T 2260 codes, per-platform tables) and the city contract every provider follows."""
import pytest

from djs import cities, providers

NAMES = providers.names()


def test_norm_strips_whitespace_and_one_suffix():
    got = [cities.norm(n) for n in ("杭州市", " 北 京 市 ", "吉林市", "海口", "乌鲁木齐市", "广东省", "", None)]
    assert got == ["杭州", "北京", "吉林", "海口", "乌鲁木齐", "广东", "", ""]


def test_norm_leaves_two_character_names_alone():
    assert cities.norm("东区") == "东区"


@pytest.mark.parametrize("typed, want", [
    ("浙江杭州", "杭州"), ("杭州市西湖区", "杭州"), ("北京市朝阳区", "北京"), ("江苏省苏州市", "苏州"), (" 杭州 ", "杭州"),
    ("杭 州", "杭州"), ("大连市中山区", "大连"),                 # the city written first, not the district named after another
    ("杭州上海", "杭州"), ("上海杭州", "上海"),                  # two cities: the first one written
    ("义乌", "义乌"), ("义乌市", "义乌"), ("浙江省", "浙江"), ("火星", "火星"),   # no known city in it: as typed, minus the suffix
    ("", ""), ("  ", ""), (None, "")])
def test_norm_resolves_a_text_that_holds_a_known_city_and_leaves_any_other_as_typed(typed, want):
    assert cities.norm(typed) == want


def test_norm_takes_the_earliest_city_and_the_longest_on_a_tie(monkeypatch):
    monkeypatch.setattr(cities, "GB", dict.fromkeys(["大", "大连", "连云港"]))
    assert cities.norm("大连") == "大连"          # an exact name beats the shorter one inside it
    assert cities.norm("连云港大连") == "连云港"    # the earliest
    assert cities.norm("大连港") == "大连"         # 大 and 大连 start together: the longer one


def test_a_loosely_written_city_reaches_every_platform_as_the_city_it_holds():
    assert {p: cities.code(p, "浙江杭州") for p in ("zhaopin", "boss", "mohrss", "iguopin", "nowcoder")} == {
        "zhaopin": "653", "boss": "101210100", "mohrss": "330100", "iguopin": "000000.330000.330100", "nowcoder": "杭州"}
    assert cities.gb_city("杭州市西湖区") == "330100" and cities.gb_province("江苏省苏州市") == "320000"
    assert providers.get("zhaopin").city_code("杭州市西湖区") == "653"
    assert cities.code("nowcoder", "义乌") == "义乌" and cities.code("zhaopin", "义乌") is None   # plain names pass, coded ones refuse


def test_gb_codes():
    assert (cities.gb_city("杭州市"), cities.gb_province("杭州")) == ("330100", "330000")
    assert (cities.gb_city("北京"), cities.gb_province("北京")) == ("110100", "110000")
    assert (cities.gb_city("东莞"), cities.gb_province("东莞")) == ("441900", "440000")
    assert cities.gb_city("火星") is None and cities.gb_province("火星") is None


def test_gb_name_resolves_a_code_of_any_level():
    assert cities.gb_name("330110") == "杭州"          # a district of 杭州
    assert cities.gb_name("110105") == "北京"
    assert cities.gb_name("429004") is None            # 仙桃 is not a canonical city
    assert cities.gb_name(None) is None and cities.gb_name("") is None


def test_code_from_the_platform_tables():
    assert {p: cities.code(p, "杭州市") for p in ("zhaopin", "boss", "job51", "yupao", "chinahr", "bytedance")} == {
        "zhaopin": "653", "boss": "101210100", "job51": "080200", "yupao": "383", "chinahr": "55", "bytedance": "CT_52"}
    assert cities.code("job58", "杭州") == cities.code("ganji", "杭州") == "79"


def test_code_for_platforms_with_a_derived_shape():
    assert cities.code("mohrss", "杭州") == "330100"                          # GB city code
    assert cities.code("iguopin", "杭州") == "000000.330000.330100"           # dotted GB path
    assert cities.code("iguopin", "北京") == "000000.110000"                  # municipalities are province-level
    assert cities.code("nowcoder", "杭州市") == "杭州" and cities.code("ciic", " 杭州 ") == "杭州"


def test_unknown_city_has_no_code_except_where_the_site_takes_any_name():
    for platform in ("zhaopin", "boss", "job51", "job58", "ganji", "yupao", "chinahr", "bytedance", "mohrss", "iguopin"):
        assert cities.code(platform, "火星") is None, platform
    assert cities.code("nowcoder", "火星") == "火星"       # the site answers an unknown name with 0 rows
    assert cities.code("nowcoder", "") is None and cities.code("no-such-platform", "杭州") is None


def test_58_group_sites_share_one_city_table():
    assert cities.supported("job58") == cities.supported("ganji")
    assert cities.code("job58", "西安") == "483" and cities.code("job58", "合肥") is None


def test_supported_lists_canonical_names_in_table_order():
    got = cities.supported("zhaopin")
    assert got[:4] == ["北京", "上海", "广州", "深圳"] and "三亚" in got and len(got) == len(cities.GB)


def test_table_rows_are_consistent():
    for name, (city, province) in cities.GB.items():
        assert len(city) == 6 and city.isdigit() and province == city[:2] + "0000", name
    for platform, table in cities.TABLES.items():
        codes = list(table.values())
        assert len(codes) == len(set(codes)), f"{platform}: two cities share a code"


# ---- the contract every provider follows
@pytest.mark.parametrize("name", NAMES)
def test_city_filter_is_declared_and_the_demo_city_works(name):
    p = providers.get(name)
    assert p.city_filter in ("server", "client", "none")
    if p.requires_city:
        assert p.city_filter == "server"
    if p.city_filter == "server":
        assert p.city_code("杭州")


def test_city_code_none_means_nationwide_unless_the_site_requires_a_city():
    assert providers.get("zhaopin").city_code(None) is None and providers.get("zhaopin").city_code("") is None
    with pytest.raises(ValueError, match="必须指定城市"):
        providers.get("job58").city_code(None)


def test_city_code_error_is_friendly_and_lists_examples():
    with pytest.raises(ValueError) as e:
        providers.get("job58").city_code("火星")
    msg = str(e.value)
    assert "58同城" in msg and "「火星」" in msg and "北京" in msg


def test_name_of_is_the_reverse_of_code():
    assert cities.name_of("city58", 79) == "杭州" and cities.name_of("ganji", "1") == "北京"   # the 58 group shares ids
    assert cities.name_of("boss", "101210100") == "杭州" and cities.name_of("zhaopin", 530) == "北京"
    assert cities.name_of("job58", 999999) is None and cities.name_of("job58", None) is None
    for platform in ("zhaopin", "boss", "job51", "city58", "yupao", "chinahr", "bytedance"):
        for name in cities.supported(platform):
            assert cities.name_of(platform, cities.code(platform, name)) == name
