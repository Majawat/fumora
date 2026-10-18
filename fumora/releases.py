"""Release-name parsing (M4, #16).

Turns titles like these into structured data:

  Absolute Batman 016 [2026] [Digital] [Shan-Empire]
  DC.Comics-Absolute.Batman.No.02.2024.HYBRID.COMIC
  Saga 001-003 (2012) (Digital-1920) (Zone-Empire)
  Saga TPB 07 by Brian K Vaughan, Fiona Staples [ENG / CBR] [VIP]
  LOCKE AND KEY - OMEGA 03 (of 07) (2013) (Digital) (Nahga-Empire)

A single issue ("016", "#19", "No.02") and a collected volume ("Vol. 3",
"TPB 07", "v02") are kept apart, so a TPB never satisfies a single issue.
Variant-cover markers don't make a separate issue.
"""

import html
import re
from dataclasses import dataclass, field
from datetime import date

from .numbers import normalize_number

EBOOK_FORMATS = {"azw3", "azw", "epub", "mobi", "kfx", "txt", "fb2"}
# PDF counts as a comic format: Fumora converts PDFs to CBZ on import.
COMIC_FORMATS = {"cbz", "cbr", "cb7", "cbt", "zip", "rar", "webp", "pdf"}
LANGUAGES = {
    "french": "fr", "german": "de", "spanish": "es", "italian": "it", "dutch": "nl", "polish": "pl",
    "portuguese": "pt", "russian": "ru", "japanese": "ja", "korean": "ko", "chinese": "zh",
    "fre": "fr", "ger": "de", "spa": "es", "ita": "it", "jpn": "ja",
}  # fmt: skip
SCENE_PUBLISHERS = (
    "dc comics", "marvel", "image comics", "image", "idw", "dark horse", "boom", "kodansha", "fantagraphics",
    "disney", "titan", "dynamite", "oni press", "viz", "yen press", "vertigo", "valiant",
)  # fmt: skip

_YEAR = re.compile(r"^(19[0-9]{2}|20[0-9]{2})(?:[-.][0-9]{1,2})?$")
_GROUP = re.compile(r"^(.+?)-(empire|dcp|novus|no ads|noads)$", re.I)
_RANGE = re.compile(r"(?<![\w.])#?\s?(\d{1,4})\s?-\s?#?(\d{1,4})(?![\w])")
_VOLUME = re.compile(
    r"(?<![\w])(?:vol(?:ume)?\.?\s*|v(?=\d)|tpb\s*|book\s*|t(?=\d{2}\b)|tome\s*)(\d{1,3})(?![\w])", re.I
)
_ISSUE = re.compile(
    r"(?<![\w.])(?:#\s?|no\.?\s?|issue\s)?(-?\d{1,4}(?:\.\d+)?|½)(?:\s?\(of\s\d+\))?(?![\w])", re.I
)


@dataclass
class ParsedRelease:
    title: str
    series: str = ""
    kind: str = "unknown"  # issue, range, volume, unknown
    number: str | None = None  # issue number or volume number
    range_start: int | None = None
    range_end: int | None = None
    volume: str | None = None  # "v3" in "Batman v3 016"
    year: int | None = None
    source: str = "unknown"  # digital, upscaled, scan, unknown
    group: str | None = None
    language: str = "en"
    formats: set[str] = field(default_factory=set)
    ebook: bool = False
    variant: bool = False
    tags: list[str] = field(default_factory=list)

    @property
    def series_key(self) -> str:
        return series_key(self.series)


def _plausible_year(text: str) -> bool:
    """A publication year, not a number like the 2099 in 'Spider-Man 2099'."""
    try:
        n = int(text)
    except ValueError:
        return False
    return 1930 <= n <= date.today().year + 1


def _pick_issue(matches: list[re.Match]) -> re.Match:
    """Of several numbers in a title, which is the issue? '#'/'No' wins, then a
    zero-padded one ('X-Men 92 001' -> 001), then the last."""
    for m in matches:
        if re.match(r"(#|no\.?|issue)", m.group(0).strip(), re.I):
            return m
    padded = [m for m in matches if re.fullmatch(r"0\d{1,3}", m.group(1))]
    if padded:
        return padded[0]
    return matches[-1] if len(matches) > 1 else matches[0]


def series_key(name: str | None) -> str:
    s = html.unescape(name or "").lower().replace("&", "and")
    s = re.sub(r"^the\s+", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def _scene_to_spaces(title: str) -> str:
    """'DC.Comics-Absolute.Batman.No.02.2024.HYBRID.COMIC' -> spaced words."""
    if " " in title or title.count(".") < 2 and title.count("_") < 2:
        return title
    out = re.sub(r"(?<=No)\.(?=\d)", " ", title)  # No.02 -> No 02
    out = re.sub(r"(\d)\.(?=(?:19|20)\d{2}(?!\d))", r"\1 ", out)  # 009.2025 -> 009 2025 (issue, year)
    out = re.sub(r"(?<!\d)\.(?!\d)|\.(?=\D)|(?<=\D)\.", " ", out)
    return out.replace("_", " ")


def parse_release(title: str) -> ParsedRelease:
    raw = html.unescape(title or "").strip()
    r = ParsedRelease(title=raw)
    text = _scene_to_spaces(raw)
    lowered = text.lower()

    # MyAnonamouse: "... by Author, Publisher [ENG / CBZ] [VIP]" -> formats, language, drop authors.
    m = re.search(r"\[([A-Z]{3})\s*/\s*([A-Z0-9 ]+)\]", text)
    if m:
        lang = m.group(1).lower()
        r.language = "en" if lang == "eng" else LANGUAGES.get(lang, lang)
        r.formats |= {f.lower() for f in m.group(2).split()}
    text = re.sub(r"\s+by\s+[^\[(]*", " ", text)

    # Bracketed tags: year, source, group, counts, formats...
    tags = re.findall(r"[\[(]([^\])]*)[\])]", text)
    r.tags = [t.strip() for t in tags if t.strip()]
    body = re.sub(r"[\[(][^\])]*[\])]", " ", text)
    for tag in r.tags:
        t = tag.lower()
        if _YEAR.match(t) and r.year is None:
            r.year = int(t[:4])
        elif re.match(r"^(19|20)\d{2}[-\s]+(19|20)\d{2}$", t):
            r.year = r.year or int(t[:4])
        if _GROUP.match(tag.strip()):
            r.group = tag.strip()
        if re.search(r"\b(\d+\s*covers?|variant|virgin|cover [a-z]|1:\d+)\b", t):
            r.variant = True
        rm = re.match(r"^#?(\d{1,4})\s?-\s?#?(\d{1,4})$", t)
        if (
            rm
            and not _YEAR.match(rm.group(1))
            and int(rm.group(1)) < int(rm.group(2))
            and r.kind == "unknown"
        ):
            # "(001-016)", "(01 - 06)": a pack of issues
            r.kind, r.range_start, r.range_end = "range", int(rm.group(1)), int(rm.group(2))
            r.number = normalize_number(rm.group(1))

    # Scene names: "Publisher-Series ... YEAR HYBRID COMIC[ eBook-GROUP]".
    sm = re.match(r"^([A-Za-z ]+?)-([A-Za-z].*)$", body.strip())
    if sm and sm.group(1).lower().strip() in SCENE_PUBLISHERS:
        body = sm.group(2)
    if re.search(r"hybrid\s+comic|retail\s+comic|hybrid\s+manga", lowered):
        # Scene group: the part after the last "-" of the original dotted name.
        if " " not in raw and (sg := re.search(r"-([A-Za-z0-9]+)$", raw)) and not sg.group(1).isdigit():
            if sg.group(1).lower() not in ("comic", "manga"):
                r.group = sg.group(1)
        body = re.sub(r"\b(hybrid|retail)\s+(comic|manga)\b.*$", " ", body, flags=re.I)
    for word in re.findall(r"[A-Za-z0-9]+", lowered):
        if word in EBOOK_FORMATS | COMIC_FORMATS:
            r.formats.add(word)
        if word in LANGUAGES:
            r.language = LANGUAGES[word]
    if any(f in EBOOK_FORMATS for f in r.formats) and not any(f in COMIC_FORMATS for f in r.formats):
        r.ebook = True
    if re.search(r"\bkindle\b|\bepub\b|\bazw3?\b", lowered):
        r.ebook = True

    # Source quality.
    if re.search(r"upscal", lowered):
        r.source = "upscaled"
    elif re.search(r"\bc2c\b|\bscan(?:s|ned)?\b|\bnoads\b|no ads", lowered):
        r.source = "scan"
    elif re.search(r"digital|webrip|web-rip|\bhybrid\b|\bretail\b|-dcp\b|\bdcp\b|digital-\d+", lowered):
        r.source = "digital"

    # Strip trailing junk words, years, extensions.
    body = re.sub(r"\.(cbz|cbr|cb7|zip|rar|pdf|epub)$", "", body.strip(), flags=re.I)
    body = re.sub(r"\b(ongoing|complete|fcbd)\b", " ", body, flags=re.I)

    # Number: range, then volume, then single issue. The series name is what precedes it.
    def take(match: re.Match, kind: str) -> str:
        r.kind = kind
        return body[: match.start()]

    head = None
    rm = _RANGE.search(body)
    if r.kind == "range":  # found in a bracket tag: the series is everything before the tags
        head = body
    elif (
        rm
        and int(rm.group(1)) < int(rm.group(2))
        and not (_plausible_year(rm.group(1)) and _plausible_year(rm.group(2)))
    ):
        head = take(rm, "range")
        r.range_start, r.range_end = int(rm.group(1)), int(rm.group(2))
        r.number = normalize_number(rm.group(1))
    else:
        vm = _VOLUME.search(body)
        issues = [
            m for m in _ISSUE.finditer(body) if not (len(m.group(1)) == 4 and _plausible_year(m.group(1)))
        ]
        if vm is not None:
            # "Batman v3 016": a volume marker followed by an issue number is an
            # issue of that volume (Mylar/Empire style), not a collected volume.
            after = [m for m in issues if m.start() >= vm.end()]
            if after:
                head = take(after[0], "issue")
                r.number = normalize_number(after[0].group(1))
                r.volume = vm.group(1)
            else:
                head = take(vm, "volume")
                r.number = normalize_number(vm.group(1))
        elif issues:
            m = _pick_issue(issues)
            head = take(m, "issue")
            r.number = normalize_number(m.group(1))
        for m in _ISSUE.finditer(body):  # a plausible year in the body
            if len(m.group(1)) == 4 and _plausible_year(m.group(1)) and r.year is None:
                r.year = int(m.group(1))
    if r.year is None:
        y = re.search(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)", body)
        if y:
            r.year = int(y.group(1))
    series = head if head is not None else body
    series = re.sub(r"\s+-\s*$|[-:#,]+\s*$", "", series.strip())
    series = re.sub(r"\s+", " ", series).strip(" -_.")
    if vm2 := re.search(r"\s+v(\d{1,4})$", series, re.I):  # "Batman v3", "Batman v2016"
        r.volume = r.volume or vm2.group(1)
        series = series[: vm2.start()].strip()
    series = re.sub(r"\s+(?:19|20)\d{2}\s*-\s*(?:19|20)\d{2}$", "", series)  # trailing year span
    if (ym := re.search(r"\s((?:19|20)\d{2})$", series)) and _plausible_year(ym.group(1)):
        # "Dark Phoenix Saga 2025" -> year (but "Spider-Man 2099" keeps its number)
        r.year = r.year or int(ym.group(1))
        series = series[: ym.start()].strip()
    if r.group is None and " " not in raw and (dg := re.search(r"[.\s]([A-Za-z0-9]+-(?:Empire|DCP))$", raw)):
        r.group = dg.group(1)
    r.series = series
    return r


def matches_issue(
    rel: ParsedRelease,
    series_names: list[str],
    number: str,
    years: tuple[int | None, ...],
    strict: bool = True,
) -> bool:
    """Does this release contain issue `number` of a series called one of
    `series_names`? Years (series start, issue cover) guard against a same-name
    series from another decade. strict=False ignores format and language (to
    tell "right issue, wrong format" from "another series")."""
    if strict and (rel.ebook or rel.language != "en"):
        return False
    if rel.series_key not in {series_key(n) for n in series_names if n}:
        return False
    want = normalize_number(number)
    if rel.kind == "issue":
        if rel.number != want:
            return False
    elif rel.kind == "range":
        try:
            n = float(want)
        except ValueError:
            return False
        if not (rel.range_start <= n <= rel.range_end):
            return False
    else:
        return False
    known = [y for y in years if y]
    if rel.year and known and not any(abs(rel.year - y) <= 1 for y in known):
        return False
    return True


def matches_volume(rel: ParsedRelease, series_names: list[str], number: str, strict: bool = True) -> bool:
    """A collected volume ("Vol. 3", "TPB 07") for a collected-edition series."""
    if rel.kind != "volume" or (strict and (rel.ebook or rel.language != "en")):
        return False
    names = {series_key(n) for n in series_names if n}
    names |= {series_key(re.sub(r"\s+(tpb|hc|omnibus)$", "", n, flags=re.I)) for n in series_names if n}
    return rel.series_key in names and rel.number == normalize_number(number)
