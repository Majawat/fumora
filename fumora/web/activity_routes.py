"""History (everything Fumora did, by series and issue) and the Blocklist (Sonarr's Activity)."""

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..importer import log
from ..models import Blocklist, Event, Issue, Series

PAGE = 100
LEVELS = ("info", "warning", "error")


def history_query(series_id: int | None = None, issue_id: int | None = None, kind: str = "", level: str = ""):
    q = select(Event)
    if issue_id:
        q = q.where(Event.issue_id == issue_id)
    elif series_id:
        q = q.where(Event.series_id == series_id)
    if kind:
        q = q.where(Event.kind == kind)
    if level in LEVELS:
        q = q.where(Event.level == level)
    return q.order_by(Event.ts.desc(), Event.id.desc())


def recent_history(db: Session, limit: int = 50, **where) -> list[Event]:
    return list(db.scalars(history_query(**where).limit(limit)))


def register_activity_routes(app: FastAPI, templates: Jinja2Templates, get_db) -> None:
    @app.get("/history", response_class=HTMLResponse)
    def history(
        request: Request,
        kind: str = "",
        level: str = "",
        series_id: int | None = None,
        page: int = 1,
        db: Session = Depends(get_db),
    ):
        page = max(page, 1)
        q = history_query(series_id=series_id, kind=kind, level=level)
        total = db.scalar(select(func.count()).select_from(q.order_by(None).subquery())) or 0
        events = db.scalars(q.offset((page - 1) * PAGE).limit(PAGE)).all()
        names = _names(db, events)
        kinds = db.scalars(select(Event.kind).distinct().order_by(Event.kind)).all()
        return templates.TemplateResponse(
            request,
            "history.html",
            {
                "events": events,
                "names": names,
                "kinds": kinds,
                "levels": LEVELS,
                "kind": kind,
                "level": level,
                "series": db.get(Series, series_id) if series_id else None,
                "page": page,
                "pages": max(1, -(-total // PAGE)),
                "total": total,
            },
        )

    @app.get("/blocklist", response_class=HTMLResponse)
    def blocklist_page(request: Request, db: Session = Depends(get_db)):
        rows = db.scalars(select(Blocklist).order_by(Blocklist.created.desc(), Blocklist.id.desc())).all()
        return templates.TemplateResponse(
            request, "blocklist.html", {"rows": rows, "names": _names(db, rows)}
        )

    @app.post("/blocklist/{row_id}/remove")
    def blocklist_remove(row_id: int, db: Session = Depends(get_db)):
        row = db.get(Blocklist, row_id)
        if row is None:
            raise HTTPException(404)
        log(db, f"Removed '{row.title}' from the blocklist", kind="grab", issue_id=row.issue_id,
            series_id=row.series_id)  # fmt: skip
        db.delete(row)
        db.commit()
        return RedirectResponse("/blocklist", status_code=303)

    @app.post("/blocklist/clear")
    def blocklist_clear(db: Session = Depends(get_db)):
        n = db.execute(delete(Blocklist)).rowcount
        log(db, f"Cleared the blocklist ({n} releases)", kind="grab")
        db.commit()
        return RedirectResponse("/blocklist", status_code=303)


def _names(db: Session, rows) -> dict:
    """Series and issue labels for rows with series_id / issue_id, in two queries."""
    sids = {r.series_id for r in rows if r.series_id}
    iids = {r.issue_id for r in rows if r.issue_id}
    series = dict(db.execute(select(Series.id, Series.name).where(Series.id.in_(sids))).all()) if sids else {}
    issues = dict(db.execute(select(Issue.id, Issue.number).where(Issue.id.in_(iids))).all()) if iids else {}
    return {"series": series, "issues": issues}
