"""Folder scan: link comic files on disk that Fumora doesn't know about yet."""

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import File, Series
from .numbers import normalize_number

COMIC_EXTS = {".cbz", ".cbr", ".cb7", ".cbt", ".pdf"}
_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}")
_NUMBER = re.compile(r"(?<![a-z0-9])#?(-?\d+(?:\.\d+)?|½)(?:[a-z]{1,3})?(?![a-z0-9])", re.IGNORECASE)
_VOLUME = re.compile(r"(?<![a-z0-9])v(?:ol(?:ume)?)?\.?\s*\d+", re.IGNORECASE)


def _key(text: str) -> str:
    text = text.lower().replace("&", "and")
    text = re.sub(r"^the\s+", "", text)
    return re.sub(r"[^a-z0-9]", "", text)


def strip_series_prefix(stem: str, series_name: str | None) -> str | None:
    """`stem` minus a leading `series_name` (compared loosely), or None if absent."""
    want = _key(series_name or "")
    if not want:
        return None
    for i in range(1, len(stem) + 1):
        if _key(stem[:i]) == want and (i == len(stem) or not stem[i].isalnum()):
            return stem[i:]
    return None


def parse_issue_number(filename: str, series_name: str | None = None) -> str | None:
    """Issue number from a file name: 'Private Dance 005 (2025).cbz' -> '005'."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = _BRACKETS.sub(" ", stem)
    if series_name:
        rest = strip_series_prefix(stem, series_name)
        if rest is not None:
            stem = rest
    stem = _VOLUME.sub(" ", stem)
    matches = [m.group(1) for m in _NUMBER.finditer(stem)]
    return matches[0] if matches else None


@dataclass
class ScanResult:
    folders: int = 0
    linked: int = 0
    unlinked: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    missing_folders: list[str] = field(default_factory=list)


def scan_folders(db: Session, series_ids: list[int] | None = None) -> ScanResult:
    """Link unknown files in series folders to issues by issue number.

    Several series can share a folder (a series and its annuals); each file is
    given to the series whose name is the longest match at the start of the file
    name, so 'X Annual 001' never lands on 'X #1'.
    """
    result = ScanResult()
    q = select(Series).where(Series.path.is_not(None))
    if series_ids:
        q = q.where(Series.id.in_(series_ids))
    by_folder: dict[str, list[Series]] = defaultdict(list)
    for s in db.scalars(q):
        by_folder[s.path].append(s)
    known = set(db.scalars(select(File.path)))

    for folder, group in by_folder.items():
        if not os.path.isdir(folder):
            result.missing_folders.append(folder)
            continue
        result.folders += 1
        # Every series that lives in this folder competes for its files.
        if series_ids:
            group = list(db.scalars(select(Series).where(Series.path == folder)))
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if path in known or os.path.splitext(name)[1].lower() not in COMIC_EXTS:
                continue
            if not os.path.isfile(path):
                continue
            stem = _BRACKETS.sub(" ", os.path.splitext(name)[0])
            candidates = [
                (len(_key(s.name)), s) for s in group if strip_series_prefix(stem, s.name) is not None
            ] + [
                (len(_key(s.mylar_name)), s)
                for s in group
                if s.mylar_name and strip_series_prefix(stem, s.mylar_name) is not None
            ]
            series = max(candidates, key=lambda c: c[0])[1] if candidates else None
            if series is None and len(group) == 1:
                series = group[0]
            number = None
            if series is not None:
                prefix = (
                    series.name if strip_series_prefix(stem, series.name) is not None else series.mylar_name
                )
                number = parse_issue_number(name, prefix)
            issue = None
            if number is not None:
                want = normalize_number(number)
                issue = next((i for i in series.issues if normalize_number(i.number) == want), None)
            if issue is not None and not issue.files:
                issue.files.append(File(path=path, size=os.path.getsize(path)))
                if issue.status != "Downloaded":
                    issue.status = "Downloaded"
                result.linked += 1
            else:
                db.add(File(issue_id=None, path=path, size=os.path.getsize(path)))
                (result.duplicates if issue is not None else result.unlinked).append(path)
            known.add(path)
            db.flush()
    return result
