from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP


_NUMBER_WITH_UNIT_RE = re.compile(
    r"(?P<number>\d+(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<unit>K|M|B|万|萬|亿|億|千|百)?",
    re.IGNORECASE,
)

_UNIT_MULTIPLIERS = {
    "k": Decimal("1000"),
    "m": Decimal("1000000"),
    "b": Decimal("1000000000"),
    "千": Decimal("1000"),
    "万": Decimal("10000"),
    "萬": Decimal("10000"),
    "亿": Decimal("100000000"),
    "億": Decimal("100000000"),
    "百": Decimal("100"),
}


def expand_compact_number(value, default: str = "") -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        return default

    match = _NUMBER_WITH_UNIT_RE.search(text.replace("，", ","))
    if not match:
        return text

    number_text = match.group("number").replace(",", "")
    unit = (match.group("unit") or "").lower()
    try:
        number = Decimal(number_text)
    except (ValueError, ArithmeticError):
        return text

    multiplier = _UNIT_MULTIPLIERS.get(unit, Decimal("1"))
    expanded = (number * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return str(int(expanded))
