"""Monitoring: per-series switches and options, per-issue toggles, Wanted page."""

from datetime import date

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import monitoring
from ..importer import log
from ..models import Issue, Series


def register_monitor_routes(app: FastAPI, templates: Jinja2Templates, get_db) -> None:
    templates.env.globals["monitor_options"] = monitoring.OPTIONS
    templates.env.globals["issue_state"] = monitoring.state

    def _series(db: Session, series_id: int) -> Series:
        s = db.get(Series, series_id)
        if s is None:
            raise HTTPException(404)
        return s

    @app.post("/series/{series_id}/monitoring")
    def series_monitoring(
        series_id: int,
        monitored: bool = Form(False),
        monitor_new: bool = Form(False),
        db: Session = Depends(get_db),
    ):
        s = _series(db, series_id)
        s.monitored, s.monitor_new = monitored, monitor_new
        db.commit()
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.post("/series/{series_id}/monitor-option")
    def series_monitor_option(series_id: int, option: str = Form(...), db: Session = Depends(get_db)):
        s = _series(db, series_id)
        if option not in monitoring.OPTIONS:
            raise HTTPException(400, "Unknown option")
        monitoring.apply_option(s, option)
        log(
            db,
            f"'{s.name}': monitoring set to '{monitoring.OPTIONS[option]}'",
            kind="monitor",
            series_id=s.id,
        )
        db.commit()
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.post("/issues/{issue_id}/monitor", response_class=HTMLResponse)
    def issue_monitor(request: Request, issue_id: int, db: Session = Depends(get_db)):
        """Toggle one issue (htmx swaps the cell; without htmx, back to the series)."""
        issue = db.get(Issue, issue_id)
        if issue is None:
            raise HTTPException(404)
        issue.monitored = not issue.monitored
        db.commit()
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(request, "_monitor_cell.html", {"i": issue})
        return RedirectResponse(f"/series/{issue.series_id}", status_code=303)

    @app.get("/wanted", response_class=HTMLResponse)
    def wanted_page(request: Request, show: str = "wanted", db: Session = Depends(get_db)):
        today = date.today()
        q = (
            select(Issue)
            .join(Series)
            .options(selectinload(Issue.series), selectinload(Issue.files))
            .order_by(func.coalesce(Issue.store_date, Issue.cover_date).desc(), Series.name)
            .limit(1000)
        )
        if show == "missing":
            q = q.where(monitoring.missing_clause(today))
        else:
            show = "wanted"
            q = q.where(monitoring.wanted_clause(today))
        issues = db.scalars(q).all()
        return templates.TemplateResponse(
            request,
            "wanted.html",
            {"issues": issues, "show": show, "counts": monitoring.counts(db, today=today)},
        )
