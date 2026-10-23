"""Library-wide pages: Library (actions, untagged issues), Jobs, and per-issue edits."""

import time

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..importer import log
from ..library_jobs import RETAG, SCAN
from ..models import File, Issue, Series, Tag
from ..ratings import METRON_RATINGS, issue_rating
from .activity_routes import recent_history


def _count(db: Session, q) -> int:
    return db.scalar(q) or 0


def library_stats(db: Session) -> dict:
    writable = or_(Series.match_status != "matched", Series.confirmed_by.is_not(None))
    files = select(func.count(File.id)).join(Issue, File.issue_id == Issue.id).join(Series)
    return {
        "series": _count(db, select(func.count(Series.id))),
        "unconfirmed": _count(
            db,
            select(func.count(Series.id)).where(
                Series.match_status == "matched", Series.confirmed_by.is_(None)
            ),
        ),
        "files": _count(db, files),
        "files_writable": _count(db, files.where(writable)),
        "files_tagged": _count(db, files.where(File.tagged_at.is_not(None))),
        "files_unlinked": _count(db, select(func.count(File.id)).where(File.issue_id.is_(None))),
    }


def preview_summary(db: Session) -> dict:
    """What the latest previews/writes found, from file.tag_preview."""
    out = {"would_change": 0, "up_to_date": 0, "errors": 0, "fields": {}}
    for (p,) in db.execute(select(File.tag_preview).where(File.tag_preview.is_not(None))):
        if p.get("error"):
            out["errors"] += 1
        elif p.get("fields") and p.get("dry_run"):
            out["would_change"] += 1
            for k in p["fields"]:
                out["fields"][k] = out["fields"].get(k, 0) + 1
        else:
            out["up_to_date"] += 1
    out["fields"] = dict(sorted(out["fields"].items(), key=lambda kv: -kv[1]))
    return out


def untagged(db: Session) -> list[dict]:
    """Series whose files get basic tags only: issues with neither a Metron
    match nor a ComicVine id (ComicVine's record gives the full tags then)."""
    rows = db.execute(
        select(Series, func.count(Issue.id))
        .join(Issue, Issue.series_id == Series.id)
        .where(Issue.metron_id.is_(None), Issue.cv_id.is_(None), Issue.files.any())
        .group_by(Series.id)
        .order_by(func.count(Issue.id).desc())
    ).all()
    return [{"series": s, "issues": n} for s, n in rows]


def register_admin_routes(app: FastAPI, templates: Jinja2Templates, get_db) -> None:
    templates.env.globals["ratings"] = METRON_RATINGS

    def _start(kind: str, params: dict) -> None:
        if app.state.jobs.active(kind) is not None:
            raise HTTPException(409, f"A {kind} job is already running; see Jobs.")
        app.state.jobs.start(kind, params)

    @app.get("/library", response_class=HTMLResponse)
    def library_page(request: Request, db: Session = Depends(get_db)):
        return templates.TemplateResponse(
            request,
            "library.html",
            {
                "stats": library_stats(db),
                "preview": preview_summary(db),
                "untagged": untagged(db),
                "running": {k: app.state.jobs.active(k) for k in (SCAN, RETAG)},
            },
        )

    @app.post("/library/retag")
    async def library_retag(write: bool = Form(False), checked: bool = Form(False)):
        if write and not checked:
            raise HTTPException(400, "Tick the box confirming you've looked at the preview.")
        _start(RETAG, {"series_ids": [], "dry_run": not write})
        return RedirectResponse("/jobs", status_code=303)

    def jobs_context() -> dict:
        lim = app.state.metron_limiter
        return {"jobs": app.state.jobs.recent(), "metron": lim, "now": time.time()}

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request):
        return templates.TemplateResponse(request, "jobs.html", jobs_context())

    @app.get("/jobs/table", response_class=HTMLResponse)
    def jobs_table(request: Request):
        return templates.TemplateResponse(request, "_jobs_table.html", jobs_context())

    @app.post("/jobs/{job_id}/cancel")
    async def job_cancel(job_id: int, db: Session = Depends(get_db)):
        if app.state.jobs.cancel(job_id):
            log(db, f"Job {job_id} cancelled", kind="jobs")
            db.commit()
        return RedirectResponse("/jobs", status_code=303)

    @app.get("/issues/{issue_id}", response_class=HTMLResponse)
    def issue_page(request: Request, issue_id: int, db: Session = Depends(get_db)):
        issue = db.scalar(
            select(Issue)
            .where(Issue.id == issue_id)
            .options(selectinload(Issue.files), selectinload(Issue.tags), selectinload(Issue.series))
        )
        if issue is None:
            raise HTTPException(404)
        return templates.TemplateResponse(
            request,
            "issue.html",
            {
                "i": issue,
                "s": issue.series,
                "effective": issue_rating(issue),
                "tag_names": ", ".join(sorted(t.name for t in issue.tags)),
                "series_tags": ", ".join(sorted(t.name for t in issue.series.tags)),
                "history": recent_history(db, 50, issue_id=issue.id),
            },
        )

    @app.post("/issues/{issue_id}/edit")
    def issue_edit(
        issue_id: int,
        age_rating: str = Form(""),
        tags: str = Form(""),
        db: Session = Depends(get_db),
    ):
        issue = db.get(Issue, issue_id)
        if issue is None:
            raise HTTPException(404)
        issue.rating_override = age_rating if age_rating in METRON_RATINGS else None
        wanted: list[str] = []
        for name in (t.strip()[:100] for t in tags.split(",")):
            if name and name.lower() not in {w.lower() for w in wanted}:
                wanted.append(name)
        new_tags = []
        for name in wanted:
            tag = db.scalar(select(Tag).where(func.lower(Tag.name) == name.lower()))
            if tag is None:
                tag = Tag(name=name)
                db.add(tag)
            new_tags.append(tag)
        issue.tags = new_tags
        db.commit()
        return RedirectResponse(f"/issues/{issue_id}", status_code=303)
