"""Series pages and the Mylar import page."""

from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from .. import naming, settings_store
from ..import_job import KIND as MYLAR_IMPORT
from ..importer import (
    apply_metron_series,
    clear_metron_values,
    import_report,
    log,
    sync_series_issues,
    unmatch_series,
)
from ..library_jobs import ORGANIZE, RETAG, SCAN, naming_settings, shared_folders
from ..metron import MetronError
from ..models import File, Issue, Series, Tag
from ..ratings import METRON_RATINGS, series_rating, strictest

MATCH_LABELS = {
    "matched": "Matched",
    "review": "Needs review",
    "not_found": "Not on Metron",
    "pending": "Not matched yet",
    "accepted": "Accepted, applying",
}


def _series_rows(db: Session, where=None, name_like: str | None = None, having_unmatched: bool = False):
    total = func.count(func.distinct(Issue.id))
    have = func.count(func.distinct(File.issue_id))
    unmatched = func.count(func.distinct(case((Issue.match_status == "unmatched", Issue.id))))
    q = (
        select(Series, total, have, unmatched)
        .outerjoin(Issue, Issue.series_id == Series.id)
        .outerjoin(File, File.issue_id == Issue.id)
        .group_by(Series.id)
        .order_by(func.lower(func.coalesce(Series.sort_name, Series.name)), Series.year_began)
    )
    if where is not None:
        q = q.where(where)
    if name_like:
        q = q.where(Series.name.ilike(f"%{name_like}%"))
    if having_unmatched:
        q = q.having(unmatched > 0)
    return [
        {"series": s, "total": t, "have": h or 0, "unmatched": u or 0} for s, t, h, u in db.execute(q).all()
    ]


def register_library_routes(app: FastAPI, templates: Jinja2Templates, get_db) -> None:
    templates.env.globals["match_labels"] = MATCH_LABELS

    @app.get("/series", response_class=HTMLResponse)
    def series_list(
        request: Request, match: str | None = None, q: str | None = None, db: Session = Depends(get_db)
    ):
        where = None
        if match in MATCH_LABELS:
            where = Series.match_status == match
        rows = _series_rows(db, where, name_like=q)
        return templates.TemplateResponse(
            request, "series_list.html", {"rows": rows, "match": match, "q": q or ""}
        )

    def detail_context(db: Session, series: Series, error: str | None = None, rename: bool = False) -> dict:
        issues = sorted(series.issues, key=lambda i: (i.sort_number is None, i.sort_number or 0, i.number))
        jobs = {kind: app.state.jobs.latest(kind) for kind in (SCAN, RETAG, ORGANIZE, "refresh")}

        def busy(kind: str) -> bool:
            j = jobs[kind]
            if j is None or j.state not in ("queued", "running"):
                return False
            ids = (j.params or {}).get("series_ids") or []
            # A refresh only blocks the page when it is refreshing this series.
            return kind != "refresh" or series.id in ids

        running = any(busy(k) for k in jobs)
        plan = None
        if rename:
            plan = naming.plan_series(
                series, shared_folder=series.path in shared_folders(db), **naming_settings(db)
            )
        return {
            "s": series,
            "issues": issues,
            "error": error,
            "jobs": jobs,
            "running": running,
            "ratings": METRON_RATINGS,
            "calculated_rating": strictest(i.rating for i in series.issues),
            "effective_rating": series_rating(series),
            "tag_names": ", ".join(sorted(t.name for t in series.tags)),
            "plan": plan,
            "writable": series.writable,
        }

    @app.get("/series/{series_id}", response_class=HTMLResponse)
    def series_detail(request: Request, series_id: int, rename: bool = False, db: Session = Depends(get_db)):
        series = db.scalar(
            select(Series)
            .where(Series.id == series_id)
            .options(selectinload(Series.issues).selectinload(Issue.files), selectinload(Series.publisher))
        )
        if series is None:
            raise HTTPException(404)
        return templates.TemplateResponse(
            request, "series_detail.html", detail_context(db, series, rename=rename)
        )

    def _get_series(db: Session, series_id: int) -> Series:
        series = db.get(Series, series_id)
        if series is None:
            raise HTTPException(404)
        return series

    @app.post("/series/{series_id}/confirm")
    def series_confirm(series_id: int, db: Session = Depends(get_db)):
        series = _get_series(db, series_id)
        if series.match_status == "matched":
            series.confirmed_by = "user"
            series.match_note = None
            log(db, f"Match confirmed for '{series.name}'", series_id=series.id)
            db.commit()
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.post("/series/{series_id}/not-on-metron")
    def series_not_on_metron(series_id: int, db: Session = Depends(get_db)):
        series = _get_series(db, series_id)
        unmatch_series(db, series, "Marked by hand as not on Metron")
        log(db, f"'{series.name}' marked as not on Metron", series_id=series.id)
        db.commit()
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.post("/series/{series_id}/edit")
    def series_edit(
        series_id: int,
        age_rating: str = Form(""),
        tags: str = Form(""),
        db: Session = Depends(get_db),
    ):
        series = _get_series(db, series_id)
        series.age_rating_override = age_rating if age_rating in METRON_RATINGS else None
        wanted = []
        for name in (t.strip() for t in tags.split(",")):
            if name and name.lower() not in {w.lower() for w in wanted}:
                wanted.append(name[:100])
        new_tags = []
        for name in wanted:
            tag = db.scalar(select(Tag).where(func.lower(Tag.name) == name.lower()))
            if tag is None:
                tag = Tag(name=name)
                db.add(tag)
            new_tags.append(tag)
        series.tags = new_tags
        db.commit()
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    def _start(kind: str, params: dict) -> None:
        active = app.state.jobs.active(kind)
        if active is not None:
            raise HTTPException(409, f"A {kind} job is already running; try again when it finishes.")
        app.state.jobs.start(kind, params)

    @app.post("/series/{series_id}/scan")
    async def series_scan(series_id: int, db: Session = Depends(get_db)):
        _get_series(db, series_id)
        _start(SCAN, {"series_ids": [series_id]})
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.post("/series/{series_id}/retag")
    async def series_retag(series_id: int, write: bool = Form(False), db: Session = Depends(get_db)):
        series = _get_series(db, series_id)
        if write and not series.writable:
            raise HTTPException(400, "Confirm the Metron match before writing tags.")
        _start(RETAG, {"series_ids": [series_id], "dry_run": not write})
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.post("/series/{series_id}/organize")
    async def series_organize(series_id: int, db: Session = Depends(get_db)):
        if not _get_series(db, series_id).writable:
            raise HTTPException(400, "Confirm the Metron match before renaming.")
        _start(ORGANIZE, {"series_ids": [series_id]})
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.post("/library/scan")
    async def library_scan():
        _start(SCAN, {})
        return RedirectResponse("/import", status_code=303)

    @app.post("/import/confirm-all")
    def import_confirm_all(db: Session = Depends(get_db)):
        n = 0
        for series in db.scalars(
            select(Series).where(Series.match_status == "matched", Series.confirmed_by.is_(None))
        ):
            series.confirmed_by = "user"
            series.match_note = None
            n += 1
        log(db, f"Confirmed {n} matches in bulk")
        db.commit()
        return RedirectResponse("/import", status_code=303)

    @app.post("/series/{series_id}/metron", response_class=HTMLResponse)
    async def series_set_metron(
        request: Request, series_id: int, metron_id: int = Form(...), db: Session = Depends(get_db)
    ):
        """Manually match a series to a Metron series id, then pair its issues."""
        series = db.get(Series, series_id)
        if series is None:
            raise HTTPException(404)
        user = settings_store.get(db, "metron.username")
        password = settings_store.get(db, "metron.password")
        error = None
        taken = db.scalar(select(Series).where(Series.metron_id == metron_id, Series.id != series.id))
        if taken is not None:
            error = f"Metron series {metron_id} is already matched to '{taken.name}'."
        elif not user or not password:
            error = "Save a Metron username and password in Settings first."
        else:
            try:
                async with app.state.make_metron_client(user, password) as client:
                    detail = await client.series(metron_id)
                    apply_metron_series(db, series, detail)
                    series.match_status, series.match_note = "matched", "Matched by hand"
                    series.suggested_metron_id = None
                    series.confirmed_by = "user"
                    # Undo the previous match: drop issues it added (if they have no
                    # files) and forget the Metron ids it assigned.
                    for issue in list(series.issues):
                        if issue.match_status == "new" and not issue.files:
                            series.issues.remove(issue)
                            db.delete(issue)
                        else:
                            clear_metron_values(issue)
                    db.flush()
                    await sync_series_issues(db, client, series)
                    series.issues_synced = datetime.now(UTC)
                log(db, f"'{series.name}' matched by hand to Metron series {metron_id}", series_id=series.id)
                db.commit()
            except MetronError as exc:
                db.rollback()
                error = str(exc)
        if error:
            series = db.get(Series, series_id)
            return templates.TemplateResponse(
                request, "series_detail.html", detail_context(db, series, error=error), status_code=400
            )
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    def status_context(db: Session) -> dict:
        """What the polled status box needs: the job and the report counts."""
        missing_files = db.scalar(
            select(func.count())
            .select_from(Issue)
            .outerjoin(File, File.issue_id == Issue.id)
            .where(Issue.status == "Downloaded", File.id.is_(None))
        )
        return {
            "job": app.state.jobs.latest(MYLAR_IMPORT),
            "report": import_report(db),
            "missing_files": missing_files,
        }

    def import_context(db: Session) -> dict:
        return {
            **status_context(db),
            "review": _series_rows(db, Series.match_status.in_(("review", "not_found", "accepted"))),
            "with_unmatched": _series_rows(db, Series.match_status == "matched", having_unmatched=True),
            "unconfirmed": db.scalars(
                select(Series)
                .where(Series.match_status == "matched", Series.confirmed_by.is_(None))
                .order_by(Series.name)
            ).all(),
            "scan_job": app.state.jobs.latest(SCAN),
            "mylar_path": settings_store.get(db, "mylar.db_path"),
        }

    # Endpoints that start jobs are async: the job runner needs the event loop.
    @app.post("/series/{series_id}/accept-suggestion")
    async def series_accept_suggestion(series_id: int, db: Session = Depends(get_db)):
        series = db.get(Series, series_id)
        if series is None or not series.suggested_metron_id:
            raise HTTPException(404)
        series.match_status = "accepted"
        db.commit()
        app.state.jobs.start(MYLAR_IMPORT, {"skip_read": True})
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.post("/import/accept-suggestions")
    async def import_accept_suggestions(db: Session = Depends(get_db)):
        for series in db.scalars(
            select(Series).where(Series.match_status == "review", Series.suggested_metron_id.is_not(None))
        ):
            series.match_status = "accepted"
        db.commit()
        app.state.jobs.start(MYLAR_IMPORT, {"skip_read": True})
        return RedirectResponse("/import", status_code=303)

    @app.get("/import", response_class=HTMLResponse)
    def import_page(request: Request, db: Session = Depends(get_db)):
        return templates.TemplateResponse(request, "import.html", import_context(db))

    @app.post("/import/start", response_class=HTMLResponse)
    async def import_start(request: Request, db: Session = Depends(get_db)):
        app.state.jobs.start(MYLAR_IMPORT)
        return templates.TemplateResponse(request, "_import_status.html", status_context(db))

    @app.get("/import/status", response_class=HTMLResponse)
    def import_status(request: Request, db: Session = Depends(get_db)):
        return templates.TemplateResponse(request, "_import_status.html", status_context(db))
