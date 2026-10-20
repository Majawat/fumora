"""Releases (Metron's schedule) and adding series from Metron."""

from datetime import date, timedelta
from itertools import groupby

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import monitoring, settings_store
from ..catalog import add_series, folder_for
from ..comicvine import ComicVineError, start_year
from ..metron import MetronError
from ..models import Issue, Publisher, Release, Series
from ..refresh import REFRESH, week_start
from ..sources import add_cv_series, gap_counts


def register_catalog_routes(app: FastAPI, templates: Jinja2Templates, get_db) -> None:
    def _client(db: Session):
        user, password, token = settings_store.metron_login(db)
        if not settings_store.metron_usable(user, password, token):
            raise HTTPException(400, "Save a Metron API token (or username and password) in Settings first.")
        return app.state.make_metron_client(user, password, token)

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
                func.coalesce(Issue.store_date, Issue.cover_date) >= date.today(),
                func.coalesce(Issue.store_date, Issue.cover_date) < date.today() + timedelta(days=60),
            )
            .options(selectinload(Issue.series), selectinload(Issue.files))
            .order_by(func.coalesce(Issue.store_date, Issue.cover_date))
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
        active = app.state.jobs.active(REFRESH)
        if active is not None and (active.params or {}).get("series_ids"):
            raise HTTPException(
                409, "A single-series refresh is running; start the full refresh when it finishes."
            )
        if active is None:
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

    def _cv(db: Session):
        key = settings_store.get(db, "comicvine.api_key")
        if not key:
            raise HTTPException(400, "Save a ComicVine API key in Settings first.")
        return app.state.make_cv_client(key)

    @app.get("/add", response_class=HTMLResponse)
    async def add_search(
        request: Request, q: str = "", source: str = "metron", db: Session = Depends(get_db)
    ):
        results, error = [], None
        source = "cv" if source == "cv" else "metron"
        if q.strip():
            try:
                if source == "cv":
                    async with _cv(db) as cv:
                        results = await cv.search_volumes(q.strip())
                else:
                    async with _client(db) as client:
                        results = await client.search_series(q.strip(), max_pages=1)
            except (MetronError, ComicVineError) as exc:
                error = str(exc)
        ours = {s.metron_id: s for s in db.scalars(select(Series).where(Series.metron_id.is_not(None)))}
        ours_cv = {s.cv_id: s for s in db.scalars(select(Series).where(Series.cv_id.is_not(None)))}
        return templates.TemplateResponse(
            request,
            "add.html",
            {"q": q, "source": source, "results": results, "ours": ours, "ours_cv": ours_cv,
             "error": error, "cv_ready": settings_store.is_set(db, "comicvine.api_key")},
        )  # fmt: skip

    @app.get("/metron-gaps", response_class=HTMLResponse)
    def metron_gaps(request: Request, db: Session = Depends(get_db)):
        """Series Fumora follows from ComicVine: candidates to add to Metron (#14),
        and Metron series the owner rejected, whose ComicVine id may be wrong there."""
        series = db.scalars(
            select(Series)
            .where(Series.source == "cv")
            .options(selectinload(Series.publisher))
            .order_by(Series.monitored.desc(), Series.sort_name, Series.name)
        ).all()
        counts = gap_counts(db)
        rows = [
            {"s": s, "issues": counts.get(s.id, (0, 0))[0], "files": counts.get(s.id, (0, 0))[1]}
            for s in series
        ]
        rejected = [r for r in rows if r["s"].rejected_metron_ids]
        return templates.TemplateResponse(request, "metron_gaps.html", {"rows": rows, "rejected": rejected})

    @app.get("/add/cv/{cv_id}", response_class=HTMLResponse)
    async def add_cv_form(request: Request, cv_id: int, db: Session = Depends(get_db)):
        existing = db.scalar(select(Series).where(Series.cv_id == cv_id))
        if existing is not None:
            return RedirectResponse(f"/series/{existing.id}", status_code=303)
        # Metron first: if it has this series, add it from there. Best effort:
        # without Metron (no login, paused) the ComicVine add still works.
        metron_note = None
        try:
            async with _client(db) as client:
                on_metron = await client.series_by_cv_id(cv_id)
            if on_metron is not None:
                return RedirectResponse(f"/add/{on_metron['id']}", status_code=303)
        except (HTTPException, MetronError) as exc:
            metron_note = f"Couldn't check Metron first ({getattr(exc, 'detail', None) or exc})."
        try:
            async with _cv(db) as cv:
                vol = await cv.volume(cv_id)
        except ComicVineError as exc:
            raise HTTPException(502, str(exc)) from exc
        if vol is None:
            raise HTTPException(404, "ComicVine has no such volume")
        pub = (vol.get("publisher") or {}).get("name")
        year = start_year(vol)
        preview = Series(
            name=vol.get("name") or "", year_began=year, publisher=Publisher(name=pub) if pub else None
        )
        return templates.TemplateResponse(
            request,
            "add_cv_form.html",
            {"v": vol, "year": year, "folder": folder_for(db, preview), "metron_note": metron_note},
        )

    @app.post("/add/cv/{cv_id}")
    async def add_cv_submit(
        cv_id: int,
        option: str = Form("future"),
        monitor_new: bool = Form(False),
        db: Session = Depends(get_db),
    ):
        if option not in monitoring.OPTIONS:
            raise HTTPException(400, "Unknown monitor option")
        try:
            async with _cv(db) as cv:
                series = await add_cv_series(db, cv, cv_id, option, monitor_new)
            db.commit()
        except ComicVineError as exc:
            db.rollback()
            raise HTTPException(502, str(exc)) from exc
        return RedirectResponse(f"/series/{series.id}", status_code=303)

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
        pub = (detail.get("publisher") or {}).get("name")
        preview = Series(
            name=detail.get("name") or "",
            year_began=detail.get("year_began"),
            series_type=(detail.get("series_type") or {}).get("name"),
            volume=detail.get("volume"),
            publisher=Publisher(name=pub) if pub else None,
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
