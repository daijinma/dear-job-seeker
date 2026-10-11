"""The data contract and the pure functions that turn what a site says into it: salary numbers, a plain-text JD,
and the list of core elements a job still lacks. Everything here is total: what cannot be read gives None / "" and
nothing is ever guessed."""
from __future__ import annotations

import html
import re

CORE = {"title": "职位名", "company": "公司", "city": "城市", "salary": "薪资", "description": "职位描述"}
UNITS = ("元/月", "元/天", "元/时", "元/年")
NO_SALARY = 9_999_999  # sites write "0 - 9999999" for "no salary"
MONTHS = range(12, 37)  # monthly payments per year worth keeping: 12 is the default, "·14薪" the usual extra

_FACTOR = {"万": 10_000, "千": 1_000, "k": 1_000, "K": 1_000}
_UNIT_OF = {"月": "元/月", "天": "元/天", "日": "元/天", "时": "元/时", "小时": "元/时", "年": "元/年"}
_PAYMENTS = re.compile(r"[·・xX×*]?\s*(\d{1,2})\s*薪")  # "·14薪": how many months are paid, never a multiplier
_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*(万|千|[kK])?(?:\s*[-~～–—－至到]\s*(\d+(?:\.\d+)?)\s*(万|千|[kK])?)?")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3})")  # 10,000
_PERIOD = re.compile(r"/\s*(月|天|日|小时|时|年)|(年|日|时)薪")
# no number to take: negotiable, one-sided ("1万以上", "3000+"), or a period we have no unit for ("/周", "/件")
_NOT_A_RANGE = re.compile(r"面议|以下|以上|起|\+|/\s*(?!月|天|日|小时|时|年)\S")


def salary_range(lo, hi, unit: str = "元/月", months=None) -> dict:
    """The four salary columns for a numeric range, or {} unless 0 < lo <= hi is a real figure in a known unit. Paid
    months only mean something for a monthly salary."""
    try:
        lo, hi = round(float(lo)), round(float(hi))
    except (TypeError, ValueError, OverflowError):
        return {}
    if unit not in UNITS or not 0 < lo <= hi < NO_SALARY:
        return {}
    return {"salary_min": lo, "salary_max": hi, "salary_unit": unit,
            "salary_months": months if unit == "元/月" and months in MONTHS else None}


def parse_salary(text) -> dict | None:
    """'15-25K·14薪' -> 15000-25000 元/月 with 14 months. Each side of a range keeps its own unit and borrows the
    other's when it has none ('1-1.5万', '8千-1.8万'); a single value is a range of one ('4000元/月'). The period
    comes from '/天' '/时' '/年' (or 日薪 时薪 年薪) and defaults to 元/月. None when there is nothing safe to take."""
    if not isinstance(text, str) or _NOT_A_RANGE.search(text):
        return None
    paid = _PAYMENTS.search(text)
    body = _THOUSANDS.sub("", _PAYMENTS.sub("", text, count=1))
    found = _RANGE.search(body)
    if not found:
        return None
    lo, lo_unit, hi, hi_unit = found.groups()
    hi = hi or lo
    lo_unit, hi_unit = lo_unit or hi_unit, hi_unit or lo_unit
    if not (lo_unit or "元" in body):  # a bare number is not a salary
        return None
    period = _PERIOD.search(body)
    unit = _UNIT_OF[period.group(1) or period.group(2)] if period else "元/月"
    return salary_range(float(lo) * _FACTOR.get(lo_unit, 1), float(hi) * _FACTOR.get(hi_unit, 1), unit,
                        int(paid.group(1)) if paid else None) or None


_DROP = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)
_BREAK = re.compile(r"<\s*br\b[^>]*>|<\s*/\s*(?:p|div|li|ul|ol|tr|h[1-6])\s*>", re.I)
_TAG = re.compile(r"</?[a-z!][^>]*>", re.I)


def plain_text(raw) -> str:
    """Third-party HTML (<br>, <p>, entities) as plain text: line breaks kept, every other tag dropped. Tags go before
    entities are decoded, so a literal '&lt;b&gt;' stays text."""
    text = _TAG.sub("", _BREAK.sub("\n", _DROP.sub("", str(raw or ""))))
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", re.sub(r"\r\n?", "\n", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def missing(fields) -> list[str]:
    """The core elements `fields` lacks, in CORE order. A salary counts only as two whole numbers."""
    def has(name: str) -> bool:
        if name == "salary":
            return all(isinstance(fields.get(k), int) for k in ("salary_min", "salary_max"))
        return bool(str(fields.get(name) or "").strip())
    return [name for name in CORE if not has(name)]
