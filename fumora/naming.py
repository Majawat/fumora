"""Sonarr-style naming templates and the rename ("organize") plan."""

import os
import re
from dataclasses import dataclass

from .comicinfo import COLLECTED_TYPES, FORMATS
from .models import Issue, Series

# Defaults reproduce Mylar's scheme, so nothing on disk changes unless the user
# chooses another template.
DEFAULT_FOLDER = "{Series Title} ({Series Year})"
DEFAULT_FILE = "{Series Title} {Issue:000} ({Issue Year})"

TOKENS = {
    "Series Title": "Private Dance",
    "Series TitleThe": "Umbrella Academy, The",
    "Series Year": "2023",
    "Volume": "1",
    "Issue": "5 (padding: {Issue:000} -> 005)",
    "Issue Title": "Story title",
    "Issue Year": "2025",
    "Issue Month": "02 ({Issue Month:00})",
    "Publisher": "Space Between Entertainment",
    "Series Type": "Single Issue",
    "Format": "Series / TPB / HC / GN ...",
    "Metron Id": "186454",
}

_TOKEN = re.compile(r"\{([A-Za-z ]+?)(?::([0#]+))?\}")
_ILLEGAL = re.compile(r'[\\/*?"<>|]')
COLON_MODES = {
    "delete": "",
    "dash": "-",
    "space_dash": " -",
    "space_dash_space": " - ",
}


def _pad(value: str, spec: str | None) -> str:
    if not spec or not value:
        return value
    m = re.fullmatch(r"(-?)(\d+)(\.\d+)?(.*)", value)
    if not m:
        return value
    sign, whole, frac, rest = m.groups()
    return f"{sign}{whole.zfill(len(spec))}{frac or ''}{rest}"


def _title_the(title: str) -> str:
    m = re.match(r"^(The|A|An)\s+(.*)$", title)
    return f"{m.group(2)}, {m.group(1)}" if m else title


def clean(name: str, colon: str = "delete") -> str:
    """Make a single path component safe on Windows/SMB."""
    name = name.replace(":", COLON_MODES.get(colon, ""))
    name = _ILLEGAL.sub("", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name or "_"


def context(series: Series, issue: Issue | None = None) -> dict[str, str]:
    cover = issue.cover_date if issue else None
    ctx = {
        "Series Title": series.name,
        "Series TitleThe": _title_the(series.name),
        "Series Year": str(series.year_began or ""),
        "Volume": str(series.volume or ""),
        "Publisher": series.publisher.name if series.publisher else "",
        "Series Type": series.series_type or "",
        "Format": FORMATS.get(series.series_type or "", ""),
    }
    if issue is not None:
        ctx.update(
            {
                "Issue": issue.number,
                "Issue Title": issue.title or "",
                "Issue Year": str(cover.year) if cover else ctx["Series Year"],
                "Issue Month": str(cover.month) if cover else "",
                "Metron Id": str(issue.metron_id or ""),
            }
        )
    return ctx


def render(template: str, ctx: dict[str, str], colon: str = "delete") -> str:
    def sub(m: re.Match) -> str:
        name, spec = m.group(1), m.group(2)
        value = ctx.get(name, "")
        return _pad(value, spec)

    text = _TOKEN.sub(sub, template)
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)  # empty "()" from a missing year
    return clean(text, colon)


@dataclass
class Rename:
    issue_id: int | None
    file_id: int | None
    old: str
    new: str


@dataclass
class OrganizePlan:
    folder: Rename | None
    files: list[Rename]
    skipped: list[str]


def plan_series(
    series: Series,
    folder_template: str = DEFAULT_FOLDER,
    file_template: str = DEFAULT_FILE,
    collected_template: str | None = None,
    colon: str = "delete",
    shared_folder: bool = False,
) -> OrganizePlan:
    """What would change for one series. The folder is only renamed when no
    other series lives in it (a series and its annuals share one)."""
    skipped: list[str] = []
    if not series.path:
        return OrganizePlan(None, [], ["Series has no folder"])
    parent = os.path.dirname(series.path.rstrip("/"))
    target_folder = os.path.join(parent, render(folder_template, context(series), colon))
    folder = None
    if target_folder != series.path and not shared_folder:
        folder = Rename(None, None, series.path, target_folder)
    elif target_folder != series.path:
        skipped.append("Folder shared with another series; only file names change")
    final_folder = folder.new if folder else series.path

    template = file_template
    if collected_template and series.series_type in COLLECTED_TYPES:
        template = collected_template
    files: list[Rename] = []
    taken: set[str] = set()
    for issue in series.issues:
        for f in issue.files:
            ext = os.path.splitext(f.path)[1].lower()
            new_name = render(template, context(series, issue), colon) + ext
            new = os.path.join(final_folder, new_name)
            old_final = os.path.join(final_folder, os.path.basename(f.path))
            if new in taken:
                skipped.append(f"Two files would be named {new_name}")
                continue
            taken.add(new)
            if new != old_final or folder:
                files.append(Rename(issue.id, f.id, f.path, new))
    files = [r for r in files if r.new != r.old]
    return OrganizePlan(folder, files, skipped)


class OrganizeError(Exception):
    pass


def apply_plan(db, series: Series, plan: OrganizePlan) -> list[str]:
    """Carry out a plan. Renames on the same filesystem keep each file's inode and
    content, so readers can recognise the moved file. Returns problems skipped."""
    problems = list(plan.skipped)
    files_by_id = {f.id: f for i in series.issues for f in i.files}
    if plan.folder:
        if os.path.exists(plan.folder.new):
            raise OrganizeError(f"Folder already exists: {plan.folder.new}")
        os.rename(plan.folder.old, plan.folder.new)
        old_prefix = plan.folder.old.rstrip("/") + "/"
        for f in files_by_id.values():
            if f.path.startswith(old_prefix):
                f.path = plan.folder.new + "/" + f.path[len(old_prefix) :]
        series.path = plan.folder.new
        db.flush()
    for r in plan.files:
        f = files_by_id.get(r.file_id)
        if f is None:
            continue
        current = f.path
        if os.path.exists(r.new):
            problems.append(f"Not renamed, target exists: {os.path.basename(r.new)}")
            continue
        os.rename(current, r.new)
        f.path = r.new
        db.flush()
    return problems
