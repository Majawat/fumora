"""Issue-number normalization shared by matching and sorting.

Issue numbers are strings: "1", "001", "1.50", "½", "-1", "258.1", "Alpha",
"1.MU". Two numbers match when their normalized forms are equal.
"""

import re

_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")
_FRACTIONS = {"½": "0.5", "¼": "0.25", "¾": "0.75"}


def normalize_number(number: str | None) -> str:
    s = (number or "").strip().lstrip("#").strip()
    s = _FRACTIONS.get(s, s)
    if _NUMERIC.match(s):
        whole, _, frac = s.partition(".")
        negative = whole.startswith("-")
        whole = whole.lstrip("-").lstrip("0") or "0"
        frac = frac.rstrip("0")
        out = whole + (f".{frac}" if frac else "")
        return f"-{out}" if negative and out != "0" else out
    return re.sub(r"\s+", "", s).lower()


def sort_number(number: str | None) -> float | None:
    """Numeric sort key: "1.MU" -> 1.0, "½" -> 0.5, "Alpha" -> None."""
    s = (number or "").strip().lstrip("#").strip()
    s = _FRACTIONS.get(s, s)
    m = re.match(r"^-?\d+(\.\d+)?", s)
    return float(m.group(0)) if m else None
