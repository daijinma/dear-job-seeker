"""Chinese city knowledge in one place: canonical names -> GB/T 2260 codes and per-platform city codes.

code(platform, name) is the value that platform's server expects for the city, or None when we have none.
Providers must refuse None (djs.providers.base.Provider.city_code): a dropped or wrong city silently becomes a
nationwide search, which is worse than an error. Shapes derived instead of tabled: nowcoder / ciic take the
plain city name, mohrss the GB city code, iguopin the dotted GB path "000000.<province>[.<city>]".
"""
from __future__ import annotations

import re

PLATFORMS = ("zhaopin", "boss", "job51", "city58", "yupao", "chinahr", "bytedance")
ALIASES = {"job58": "city58", "ganji": "city58"}     # both sites use the same 58-group city ids
BY_NAME = ("nowcoder", "ciic")
MUNICIPALITIES = ("北京", "上海", "天津", "重庆")    # province-level in iguopin's dotted path

# One city per row, "-" = the platform has no code for it. Platform codes come from each site's own city
# dictionary; zhaopin uses the province-ordered list (565+), municipalities exist only in its 530-551 hot list.
# Add a city by adding a row.
TABLE = """
# name     gb_city gb_prov zhaopin boss      job51  city58 yupao chinahr bytedance
北京     110100  110000  530     101010100 010000 1      2     5       CT_11
上海     310100  310000  538     101020100 020000 2      25    115     CT_125
广州     440100  440000  763     101280100 030200 3      76    40      CT_45
深圳     440300  440000  765     101280600 040000 4      77    125     CT_128
杭州     330100  330000  653     101210100 080200 79     383   55      CT_52
成都     510100  510000  801     101270100 090200 102    322   20      CT_22
南京     320100  320000  635     101190100 070200 172    220   100     CT_107
武汉     420100  420000  736     101200100 180200 158    180   150     CT_154
西安     610100  610000  854     101110100 200200 483    311   160     CT_155
苏州     320500  320000  639     101190400 070300 5      221   220     CT_199
天津     120100  120000  531     101030100 050000 18     27    140     CT_143
重庆     500100  500000  551     101040100 060000 37     32    25      CT_190
长沙     430100  430000  749     101250100 190200 414    197   15      CT_20
郑州     410100  410000  719     101180100 170200 342    149   175     CT_188
青岛     370200  370000  703     101120200 120300 122    284   110     CT_119
济南     370100  370000  702     101120100 120200 265    283   75      CT_66
合肥     340100  340000  664     101220100 150200 -      3401  65      CT_55
厦门     350200  350000  682     101230200 110300 606    60    155     CT_158
福州     350100  350000  681     101230100 110200 304    53    35      CT_40
宁波     330200  330000  654     101210400 080300 135    388   107     CT_112
东莞     441900  440000  779     101281600 030800 413    79    225     CT_36
佛山     440600  440000  768     101280800 030600 222    80    16050   CT_39
无锡     320200  320000  636     101190200 070400 93     222   152     CT_151
昆明     530100  530000  831     101290100 250200 541    367   80      CT_80
沈阳     210100  210000  599     101070100 230200 188    244   120     CT_129
大连     210200  210000  600     101070200 230300 147    245   30      CT_25
哈尔滨   230100  230000  622     101050100 220200 202    167   60      CT_48
长春     220100  220000  613     101060100 240200 319    211   10      CT_19
石家庄   130100  130000  565     101090100 160200 241    138   130     CT_133
太原     140100  140000  576     101100100 210200 -      300   135     CT_141
南昌     360100  360000  691     101240100 130200 -      233   95      CT_105
南宁     450100  450000  785     101300100 140200 -      97    105     CT_108
贵阳     520100  520000  822     101260100 260200 -      111   45      CT_47
兰州     620100  620000  864     101160100 270200 -      62    85      CT_83
海口     460100  460000  799     101310100 100200 -      120   50      CT_49
乌鲁木齐 650100  650000  890     101130100 310200 -      351   145     CT_150
珠海     440400  440000  766     101280700 030500 -      96    180     CT_192
惠州     441300  440000  773     101280300 030300 -      82    16020   CT_63
中山     442000  440000  780     101281700 030700 -      95    16070   CT_189
温州     330300  330000  655     101210700 080400 330    391   147     CT_149
嘉兴     330400  330000  656     101210300 080700 497    385   73      CT_68
绍兴     330600  330000  658     101210500 080500 355    389   8050    CT_127
金华     330700  330000  659     101210900 080600 531    386   8060    CT_71
常州     320400  320000  638     101191100 070500 463    223   18      CT_18
南通     320600  320000  641     101190500 070900 394    226   7060    CT_110
徐州     320300  320000  637     101190800 071100 471    229   7030    CT_167
扬州     321000  320000  645     101190600 070800 637    231   167     CT_172
泉州     350500  350000  685     101230500 110400 291    58    10030   CT_121
烟台     370600  370000  707     101120500 120400 228    297   168     CT_170
潍坊     370700  370000  708     101120600 120500 362    296   12050   CT_147
洛阳     410300  410000  721     101180900 170300 556    150   92      CT_95
唐山     130200  130000  566     101090500 160500 276    146   1020    CT_142
呼和浩特 150100  150000  587     101080100 280200 -      258   70      CT_59
银川     640100  640000  886     101170100 290200 -      270   170     CT_177
西宁     630100  630000  878     101150100 320200 -      275   165     CT_156
拉萨     540100  540000  847     101140100 300200 -      344   90      CT_82
三亚     460200  460000  800     101310200 100300 -      121   18020   CT_100065
"""


def _load() -> tuple[dict, dict]:
    gb, tables = {}, {p: {} for p in PLATFORMS}
    for line in TABLE.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, city, province, *codes = line.split()
        gb[name] = (city, province)
        for platform, c in zip(PLATFORMS, codes):
            if c != "-":
                tables[platform][name] = c
    return gb, tables


GB, TABLES = _load()                                   # GB: name -> (city code, province code)
_BY_GB = {city[:4]: name for name, (city, _) in GB.items()}


def norm(name: str | None) -> str:
    """'杭州市' -> '杭州': drop whitespace and one 市/省/区 suffix (never from a 2-character name)."""
    n = re.sub(r"\s+", "", name or "")
    return n[:-1] if len(n) > 2 and n[-1] in "市省区" else n


def gb_city(name: str) -> str | None:
    return GB.get(norm(name), (None, None))[0]


def gb_province(name: str) -> str | None:
    return GB.get(norm(name), (None, None))[1]


def gb_name(gb_code: str | None) -> str | None:
    """Canonical city that contains a GB/T 2260 code of any level (district 330110 -> 杭州), else None."""
    return _BY_GB.get(str(gb_code or "")[:4])


def code(platform: str, name: str) -> str | None:
    n = norm(name)
    if platform in BY_NAME:
        return n or None
    if platform == "mohrss":
        return gb_city(n)
    if platform == "iguopin":
        city, province = gb_city(n), gb_province(n)
        if city is None:
            return None
        return f"000000.{province}" if n in MUNICIPALITIES else f"000000.{province}.{city}"
    return TABLES.get(ALIASES.get(platform, platform), {}).get(n)


def name_of(platform: str, platform_code) -> str | None:
    """The canonical city a platform's own city code stands for (the reverse of code()), else None."""
    table = TABLES.get(ALIASES.get(platform, platform), {})
    return next((name for name, c in table.items() if c == str(platform_code)), None)


def supported(platform: str) -> list[str]:
    """Canonical cities the platform can be searched by (examples only for the BY_NAME sites, which take any name)."""
    return [n for n in GB if code(platform, n)]
