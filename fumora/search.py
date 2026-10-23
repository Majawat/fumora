"""Choosing a release for an issue (M4): match, filter, score.

The profile (#3): native digital > unknown > HD-Upscaled > scans; size limits;
ignore words; preferred/blocked groups; usenet before torrents; single issues
before packs.
"""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from . import settings_store
from .comicinfo import COLLECTED_TYPES
from .models import Issue
from .numbers import normalize_number
from .releases import (
    LANGUAGES,
    ParsedRelease,
    matches_issue,
    matches_single_volume,
    matches_volume,
    parse_release,
    series_key,
)

SOURCE_SCORE = {"digital": 300, "unknown": 200, "upscaled": 150, "scan": 100}
MB = 1024 * 1024


@dataclass
class Profile:
    min_mb: float = 5
    max_mb: float = 300
    max_collected_mb: float = 2048
    # Same default as the quality.ignore_words setting.
    ignore_words: list[str] = field(
        default_factory=lambda: [".exe", ".iso", "pdf-xpost", "preview", "sample"]
    )
    preferred_groups: list[str] = field(default_factory=list)
    blocked_groups: list[str] = field(default_factory=list)
    protocols: list[str] = field(default_factory=lambda: ["usenet", "torrent", "ddl"])
    allow_packs: bool = True
    languages: list[str] = field(default_factory=lambda: ["en"])
    other_languages: str = "ask"  # ask: hold for the owner's OK; reject: never


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def load_profile(db: Session) -> Profile:
    p = Profile()

    def num(key: str, default: float) -> float:
        try:
            return float(settings_store.get(db, key) or default)
        except ValueError:
            return default

    p.min_mb = num("quality.min_mb", p.min_mb)
    p.max_mb = num("quality.max_mb", p.max_mb)
    p.max_collected_mb = num("quality.max_collected_mb", p.max_collected_mb)
    if (words := settings_store.get(db, "quality.ignore_words")) is not None:
        p.ignore_words = _list(words)
    p.preferred_groups = _list(settings_store.get(db, "quality.preferred_groups"))
    p.blocked_groups = _list(settings_store.get(db, "quality.blocked_groups"))
    p.protocols = [x.lower() for x in _list(settings_store.get(db, "quality.protocols"))] or p.protocols
    p.languages = [language_code(x) for x in _list(settings_store.get(db, "search.languages"))] or p.languages
    p.other_languages = settings_store.get(db, "search.other_languages") or p.other_languages
    return p


def language_code(value: str | None) -> str:
    """'en', 'EN', 'English', 'eng' -> 'en'."""
    v = (value or "").strip().lower()
    return _LANGUAGE_CODES.get(v) or LANGUAGES.get(v) or v


_LANGUAGE_CODES = {
    "eng": "en", "english": "en", "fra": "fr", "deu": "de", "pol": "pl", "por": "pt", "rus": "ru",
    "chi": "zh", "zho": "zh", "kor": "ko", "dut": "nl", "nld": "nl", "swe": "sv", "swedish": "sv",
    "cze": "cs", "ces": "cs", "czech": "cs", "hun": "hu", "hungarian": "hu", "tur": "tr", "turkish": "tr",
    "croatian": "hr", "hrv": "hr", "serbian": "sr", "srp": "sr", "greek": "el", "gre": "el", "ell": "el",
}  # fmt: skip


@dataclass
class Candidate:
    # Prowlarr-shaped result (Prowlarr, GetComics, Shelfmark); its downloadUrl stays server-side.
    release: dict
    parsed: ParsedRelease
    score: int = 0
    rejections: list[str] = field(default_factory=list)
    # The right series and issue, whatever the profile says about it.
    identified: bool = False
    blocklisted: bool = False
    # Fine except for something the owner may OK by hand (a language not in the list).
    hold: str | None = None

    @property
    def accepted(self) -> bool:
        return not self.rejections and self.hold is None

    @property
    def holdable(self) -> bool:
        return not self.rejections and self.hold is not None

    @property
    def language(self) -> str:
        return language_code(self.release.get("language")) or self.parsed.language

    @property
    def size_mb(self) -> float:
        return (self.release.get("size") or 0) / MB

    @property
    def protocol(self) -> str:
        return self.release.get("protocol") or ""

    @property
    def age_days(self) -> int | None:
        try:
            published = datetime.fromisoformat(self.release["publishDate"].replace("Z", "+00:00"))
        except (KeyError, ValueError, AttributeError):
            return None
        return (datetime.now(UTC) - published).days


def _group_in(group: str | None, title: str, names: list[str]) -> bool:
    hay = f"{group or ''} {title}".lower()
    return any(n.lower() in hay for n in names)


def evaluate(release: dict, issue: Issue, profile: Profile) -> Candidate:
    rel = parse_release(release.get("title") or "")
    c = Candidate(release=release, parsed=rel)
    series = issue.series
    names = [n for n in (series.name, series.mylar_name) if n]
    collected = series.series_type in COLLECTED_TYPES
    as_volumes = not collected and series.issues_are_volumes  # manga: "Vol. 12" is issue 12
    # New issues often have a store date but no cover date yet: use whichever is known.
    years = (
        series.year_began,
        issue.cover_date.year if issue.cover_date else None,
        issue.store_date.year if issue.store_date else None,
    )

    one_volume = collected and len(series.issues) == 1 and normalize_number(issue.number) == "1"
    if one_volume and matches_single_volume(rel, names, years):
        c.identified = True  # a graphic novel released without a number
    elif collected:
        # A collected edition only takes collected volumes, never single issue #N.
        c.identified = matches_volume(rel, names, issue.number, strict=False)
        if not matches_volume(rel, names, issue.number):
            c.rejections.append("not this volume" if not c.identified else "ebook")
    elif as_volumes and matches_volume(rel, names, issue.number, strict=False):
        c.identified = True
        if rel.ebook:
            c.rejections.append("ebook, not a comic archive")
    elif not matches_issue(rel, names, issue.number, years):
        c.identified = matches_issue(rel, names, issue.number, years, strict=False)
        if rel.ebook:
            c.rejections.append("ebook, not a comic archive")
        elif rel.kind in ("volume", "volumes"):
            c.rejections.append("a collected volume, not this issue")
        else:
            c.rejections.append("not this issue")
    else:
        c.identified = True

    holds = []
    if c.identified and rel.alt_series and series_key(rel.series) not in {series_key(n) for n in names}:
        # Only the second reading matched ("Miles.Morales-Spider-Man.017" read as "Spider-Man"):
        # a writer and a character look alike in a name, so the owner decides.
        holds.append(f"matches only as '{rel.alt_series}' (from '{rel.series}')")
    if c.language not in profile.languages:
        if profile.other_languages == "reject":
            c.rejections.append(f"language: {c.language}")
        else:
            holds.append(f"language: {c.language}")
    c.hold = "; ".join(holds) or None

    size = c.size_mb
    pack = rel.kind in ("range", "volumes")
    limit = profile.max_collected_mb if (collected or as_volumes or pack) else profile.max_mb
    if size and size < profile.min_mb:
        c.rejections.append(f"too small ({size:.0f} MB)")
    if size > limit:
        c.rejections.append(f"too big ({size:.0f} MB > {limit:.0f})")
    title_l = (release.get("title") or "").lower()
    if re.search(r"\bcovers?\s+only\b", title_l):  # Libgen: "(Cover B - ... Variant) (Cover ONLY)"
        c.rejections.append("only the cover")
    if hit := next((w for w in profile.ignore_words if w.lower() in title_l), None):
        c.rejections.append(f"ignored word: {hit}")
    if profile.blocked_groups and _group_in(rel.group, title_l, profile.blocked_groups):
        c.rejections.append("blocked group")
    if c.protocol not in profile.protocols:
        c.rejections.append(f"{c.protocol} disabled")
    if c.protocol == "torrent" and (release.get("seeders") or 0) == 0:
        c.rejections.append("no seeders")
    if pack and not profile.allow_packs:
        c.rejections.append("packs disabled")

    score = SOURCE_SCORE.get(rel.source, 200)
    if "mobile" in title_l:  # "digital-mobile": smaller, phone-sized pages
        score -= 50
    if c.protocol in profile.protocols:
        score += 60 * (len(profile.protocols) - profile.protocols.index(c.protocol))
    if profile.preferred_groups and _group_in(rel.group, title_l, profile.preferred_groups):
        score += 100
    if rel.kind == "issue":
        score += 40
    if c.protocol == "torrent":
        score += min(30, int(10 * math.log10((release.get("seeders") or 0) + 1)))
    c.score = score
    return c


def rank(candidates: list[Candidate]) -> list[Candidate]:
    """Accepted first, then those held for an OK, best score first; ties go to the
    larger (more complete) file."""
    return sorted(
        candidates,
        key=lambda c: (not c.accepted, not c.holdable, -c.score, -(c.release.get("size") or 0)),
    )


def wants(issues: list[Issue]) -> Callable[[str], bool]:
    """Is a title (e.g. a GetComics post) for one of these issues, whatever its format?"""
    loose = Profile()

    def want(title: str) -> bool:
        return any(evaluate({"title": title}, i, loose).identified for i in issues)

    return want


def queries_for(issue: Issue) -> list[str]:
    """One search per series name: indexers return many issues at once."""
    return [n for n in dict.fromkeys([issue.series.name, issue.series.mylar_name]) if n]


def fallback_queries(issue: Issue) -> list[str]:
    """Issue-specific searches, for when the series-name results (newest first,
    capped by indexers) don't reach an older issue."""
    n = issue.number
    padded = n.zfill(3) if n.isdigit() else n
    return [f"{name} {padded}" for name in queries_for(issue)]
