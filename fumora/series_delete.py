"""Deleting a series (Sonarr's "Delete series"): the record always, its files only when asked.

Files go to the recycle folder, or are deleted for good when the owner turned it off
(`recycle.recycle_dir`). The series folder goes as a whole only when it is the series'
own: never the library root, an import folder, the recycle or download folder, a folder
above any of them, or a folder another series uses. Otherwise only the series' own files
go. Files go first; if one can't be removed, nothing is taken out of the database, so the
delete can simply be tried again.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import settings_store
from .grabbing import ACTIVE, APPROVAL
from .importer import log
from .models import File, Grab, Issue, Job, Series
from .recycle import DEFAULT_RECYCLE, discard


class DeleteFailed(Exception):
    pass


@dataclass
class DeletePlan:
    folder: str | None = None  # removed as a whole
    files: list[str] = field(default_factory=list)  # removed one by one
    folder_note: str | None = None  # why the folder isn't removed as a whole
    blockers: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.files) + (1 if self.folder else 0)


def _real(p: str) -> str:
    return os.path.normpath(os.path.realpath(p))


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def _protected(db: Session) -> list[str]:
    keys = ("library.path", "library.recycle_path", "ddl.path")
    paths = [settings_store.get(db, k) for k in keys]
    paths.append(DEFAULT_RECYCLE)
    paths += [r for r in (settings_store.get(db, "import.roots") or "").split(",")]
    return [_real(p.strip()) for p in paths if p and p.strip()]


def active_grabs(db: Session, series: Series) -> list[Grab]:
    ids = {i.id for i in series.issues}
    out = []
    for g in db.scalars(select(Grab).where(Grab.state.in_((*ACTIVE, APPROVAL)))):
        if g.series_id == series.id or ids & set(g.issue_ids or [g.issue_id]):
            out.append(g)
    return out


def busy_jobs(db: Session, series: Series) -> list[Job]:
    """Running jobs that may touch this series: library-wide ones, or ones naming it."""
    ids = {i.id for i in series.issues}
    out = []
    for j in db.scalars(select(Job).where(Job.state.in_(("queued", "running")))):
        p = j.params or {}
        if p.get("series_ids"):
            if series.id in p["series_ids"]:
                out.append(j)
        elif p.get("issue_ids"):
            if ids & set(p["issue_ids"]):
                out.append(j)
        else:
            out.append(j)
    return out


def plan_delete(db: Session, series: Series) -> DeletePlan:
    plan = DeletePlan()
    for j in busy_jobs(db, series):
        plan.blockers.append(f"A {j.kind} job is running (see Jobs): try again when it finishes.")
    for g in active_grabs(db, series):
        plan.blockers.append(
            f"'{g.release_title}' is {'waiting for your OK' if g.state == APPROVAL else g.state} "
            "on the Queue page: remove it there first."
        )
    files = [f.path for i in series.issues for f in i.files]
    folder = _real(series.path) if series.path else None
    if folder and not os.path.isdir(folder):
        folder = None
    if folder:
        others = db.scalars(select(Series.path).where(Series.id != series.id, Series.path.is_not(None))).all()
        other_files = db.scalars(
            select(File.path).join(Issue, File.issue_id == Issue.id).where(Issue.series_id != series.id)
        ).all()
        protected = next((p for p in _protected(db) if _inside(p, folder)), None)
        if folder == "/" or protected:
            plan.folder_note = f"{folder} is (or holds) {protected or '/'}: only the series' own files go."
        elif any(_inside(_real(o), folder) or _inside(folder, _real(o)) for o in others):
            plan.folder_note = f"Another series uses {folder} too: only this series' files go."
        elif any(_inside(_real(f), folder) for f in other_files):
            plan.folder_note = f"{folder} holds another series' files: only this series' files go."
        else:
            plan.folder = folder
    plan.files = [f for f in files if not (plan.folder and _inside(_real(f), plan.folder))]
    return plan


def delete_series(db: Session, series: Series, delete_files: bool, recycle: Path | None) -> str:
    """Delete `series` (and, if asked, its files). Returns a one-line summary."""
    plan = plan_delete(db, series)
    if plan.blockers:
        raise DeleteFailed(" ".join(plan.blockers))
    name, gone = series.name, 0
    if delete_files:
        targets = ([plan.folder] if plan.folder else []) + plan.files
        for t in targets:
            if not os.path.lexists(t):
                continue  # already gone (e.g. a retry after a partial delete)
            try:
                discard(Path(t), recycle)
            except OSError as exc:
                raise DeleteFailed(
                    f"Couldn't remove {t}: {exc.strerror or exc}. Nothing was taken out of Fumora; "
                    f"{gone} of {len(targets)} removed so far."
                ) from exc
            gone += 1
    for issue in series.issues:
        for f in list(issue.files):
            db.delete(f)  # the file records go with the series (a later scan finds kept files again)
    db.delete(series)
    where = f"to the recycle folder {recycle}" if recycle is not None else "deleted for good"
    if not delete_files:
        summary = f"Deleted series '{name}' from Fumora; its files were kept"
    elif plan.folder:
        summary = f"Deleted series '{name}' and its folder {plan.folder} ({where})"
    else:
        summary = f"Deleted series '{name}' and {gone} of its files ({where})"
    log(db, summary, kind="series")
    db.commit()
    return summary
