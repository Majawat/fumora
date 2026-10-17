"""Releases (Metron's schedule) and adding series from Metron."""

from datetime import date, timedelta
from itertools import groupby

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import monitoring, settings_store
from ..catalog import add_series, folder_for
from ..metron import MetronError
from ..models import Issue, Release, Series
from ..refresh import REFRESH, week_start


def register_catalog_routes(app: FastAPI, templates: Jinja2Templates, get_db) -> None:
    def _client(db: Session):
        user = settings_store.get(db, "metron.username")
        password = settings_store.get(db, "metron.password")
        if not user or not password:
            raise HTTPException(400, "Save a Metron username and password in Settings first.")
        return app.state.make_metron_client(user, password)

    @app.get("/releases", response_class=HTMLResponse)
    def releases_page(request: Request, week: str | None = None, db: Session = Depends(get_db)):
        try:
            start = week_start(date.fromisoformat(week)) if week else week_start(date.today())
        except ValueError:
            start = week_start(date.today())
        end = start + timedelta(days=7)
        rows = db.scalars(
            select(Release)
            .where(Release.store_date >= start, Release.store_date < end)
            .order_by(Release.store_date, Release.series_name, Release.number)
        ).all()
        ours = {s.metron_id: s for s in db.scalars(select(Series).where(Series.metron_id.is_not(None)))}
        issues = {
            i.metron_id: i
            for i in db.scalars(
                select(Issue)
                .where(Issue.metron_id.in_([r.metron_issue_id for r in rows]))
                .options(selectinload(Issue.files), selectinload(Issue.series))
            )
        }
        mine = [r for r in rows if r.metron_series_id in ours]
        days = [(d, list(g)) for d, g in groupby(rows, key=lambda r: r.store_date)]
        upcoming = db.scalars(
            select(Issue)
            .join(Series)
            .where(
                Series.monitored.is_(True),
                Issue.monitored.is_(True),
                Issue.store_date >= date.today(),
                Issue.store_date < date.today() + timedelta(days=60),
            )
            .options(selectinload(Issue.series), selectinload(Issue.files))
            .order_by(Issue.store_date)
        ).all()
        return templates.TemplateResponse(
            request,
            "releases.html",
            {
                "start": start,
                "prev": start - timedelta(days=7),
                "next": start + timedelta(days=7),
                "days": days,
                "mine": mine,
                "ours": ours,
                "issues": issues,
                "total": len(rows),
                "upcoming": upcoming,
                "last_refresh": settings_store.get_state(db, "refresh.last_success"),
                "refresh_job": app.state.jobs.latest(REFRESH),
            },
        )

    @app.post("/refresh")
    async def refresh_now():
        if app.state.jobs.active(REFRESH) is None:
            app.state.jobs.start(REFRESH, {})
        return RedirectResponse("/jobs", status_code=303)

    @app.post("/series/{series_id}/refresh")
    async def refresh_one(series_id: int, db: Session = Depends(get_db)):
        s = db.get(Series, series_id)
        if s is None or s.metron_id is None:
            raise HTTPException(404)
        if app.state.jobs.active(REFRESH) is not None:
            raise HTTPException(409, "A refresh is already running; see Jobs.")
        app.state.jobs.start(REFRESH, {"series_ids": [series_id]})
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.get("/add", response_class=HTMLResponse)
    async def add_search(request: Request, q: str = "", db: Session = Depends(get_db)):
        results, error = [], None
        if q.strip():
            try:
                async with _client(db) as client:
                    results = await client.search_series(q.strip(), max_pages=1)
            except MetronError as exc:
                error = str(exc)
        ours = {s.metron_id: s for s in db.scalars(select(Series).where(Series.metron_id.is_not(None)))}
        return templates.TemplateResponse(
            request, "add.html", {"q": q, "results": results, "ours": ours, "error": error}
        )

    @app.get("/add/{metron_id}", response_class=HTMLResponse)
    async def add_form(request: Request, metron_id: int, db: Session = Depends(get_db)):
        existing = db.scalar(select(Series).where(Series.metron_id == metron_id))
        if existing is not None:
            return RedirectResponse(f"/series/{existing.id}", status_code=303)
        try:
            async with _client(db) as client:
                detail = await client.series(metron_id)
        except MetronError as exc:
            raise HTTPException(502, str(exc)) from exc
        preview = Series(
            name=detail.get("name") or "",
            year_began=detail.get("year_began"),
            series_type=(detail.get("series_type") or {}).get("name"),
            volume=detail.get("volume"),
        )
        return templates.TemplateResponse(
            request, "add_form.html", {"d": detail, "folder": folder_for(db, preview)}
        )

    @app.post("/add/{metron_id}")
    async def add_submit(
        metron_id: int,
        option: str = Form("future"),
        monitor_new: bool = Form(False),
        db: Session = Depends(get_db),
    ):
        if option not in monitoring.OPTIONS:
            raise HTTPException(400, "Unknown monitor option")
        try:
            async with _client(db) as client:
                series = await add_series(db, client, metron_id, option, monitor_new)
            db.commit()
        except MetronError as exc:
            db.rollback()
            raise HTTPException(502, str(exc)) from exc
        return RedirectResponse(f"/series/{series.id}", status_code=303)
