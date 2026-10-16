"""Series pages and the Mylar import page."""

from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from .. import settings_store
from ..import_job import KIND as MYLAR_IMPORT
from ..importer import apply_metron_series, import_report, log, sync_series_issues
from ..metron import MetronError
from ..models import File, Issue, Series

MATCH_LABELS = {
    "matched": "Matched",
    "review": "Needs review",
    "not_found": "Not on Metron",
    "pending": "Not matched yet",
    "accepted": "Accepted, applying",
}


def _series_rows(db: Session, where=None):
    have = func.sum(case((Issue.status == "Downloaded", 1), else_=0))
    unmatched = func.sum(case((Issue.match_status == "unmatched", 1), else_=0))
    q = (
        select(Series, func.count(Issue.id), have, unmatched)
        .outerjoin(Issue, Issue.series_id == Series.id)
        .group_by(Series.id)
        .order_by(func.lower(func.coalesce(Series.sort_name, Series.name)), Series.year_began)
    )
    if where is not None:
        q = q.where(where)
    return [
        {"series": s, "total": total, "have": have or 0, "unmatched": unmatched or 0}
        for s, total, have, unmatched in db.execute(q).all()
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
        rows = _series_rows(db, where)
        if q:
            needle = q.lower()
            rows = [r for r in rows if needle in r["series"].name.lower()]
        return templates.TemplateResponse(
            request, "series_list.html", {"rows": rows, "match": match, "q": q or ""}
        )

    @app.get("/series/{series_id}", response_class=HTMLResponse)
    def series_detail(request: Request, series_id: int, db: Session = Depends(get_db)):
        series = db.scalar(
            select(Series)
            .where(Series.id == series_id)
            .options(selectinload(Series.issues).selectinload(Issue.files), selectinload(Series.publisher))
        )
        if series is None:
            raise HTTPException(404)
        issues = sorted(
            series.issues,
            key=lambda i: (i.sort_number is None, i.sort_number or 0, i.number),
        )
        return templates.TemplateResponse(
            request, "series_detail.html", {"s": series, "issues": issues, "error": None}
        )

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
                    # Undo the previous match: drop issues it added (if they have no
                    # files) and forget the Metron ids it assigned.
                    for issue in list(series.issues):
                        if issue.match_status == "new" and not issue.files:
                            series.issues.remove(issue)
                            db.delete(issue)
                        else:
                            issue.metron_id = None
                            issue.match_status = None
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
            issues = sorted(
                series.issues, key=lambda i: (i.sort_number is None, i.sort_number or 0, i.number)
            )
            return templates.TemplateResponse(
                request,
                "series_detail.html",
                {"s": series, "issues": issues, "error": error},
                status_code=400,
            )
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    def import_context(db: Session) -> dict:
        job = app.state.jobs.latest(MYLAR_IMPORT)
        review = _series_rows(db, Series.match_status.in_(("review", "not_found", "accepted")))
        with_unmatched = [r for r in _series_rows(db, Series.match_status == "matched") if r["unmatched"]]
        missing_files = db.scalar(
            select(func.count())
            .select_from(Issue)
            .outerjoin(File, File.issue_id == Issue.id)
            .where(Issue.status == "Downloaded", File.id.is_(None))
        )
        return {
            "job": job,
            "report": import_report(db),
            "review": review,
            "with_unmatched": with_unmatched,
            "missing_files": missing_files,
            "mylar_path": settings_store.get(db, "mylar.db_path"),
        }

    @app.post("/series/{series_id}/accept-suggestion")
    def series_accept_suggestion(series_id: int, db: Session = Depends(get_db)):
        series = db.get(Series, series_id)
        if series is None or not series.suggested_metron_id:
            raise HTTPException(404)
        series.match_status = "accepted"
        db.commit()
        app.state.jobs.start(MYLAR_IMPORT)
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.post("/import/accept-suggestions")
    def import_accept_suggestions(db: Session = Depends(get_db)):
        for series in db.scalars(
            select(Series).where(Series.match_status == "review", Series.suggested_metron_id.is_not(None))
        ):
            series.match_status = "accepted"
        db.commit()
        app.state.jobs.start(MYLAR_IMPORT)
        return RedirectResponse("/import", status_code=303)

    @app.get("/import", response_class=HTMLResponse)
    def import_page(request: Request, db: Session = Depends(get_db)):
        return templates.TemplateResponse(request, "import.html", import_context(db))

    @app.post("/import/start", response_class=HTMLResponse)
    def import_start(request: Request, db: Session = Depends(get_db)):
        app.state.jobs.start(MYLAR_IMPORT)
        return templates.TemplateResponse(request, "_import_status.html", import_context(db))

    @app.get("/import/status", response_class=HTMLResponse)
    def import_status(request: Request, db: Session = Depends(get_db)):
        return templates.TemplateResponse(request, "_import_status.html", import_context(db))
