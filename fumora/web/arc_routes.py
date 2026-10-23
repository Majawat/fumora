"""Story arcs: search Metron, follow one, see what you have, monitor, add series, export .cbl."""

from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import arcs, settings_store
from ..catalog import add_series
from ..importer import log
from ..metron import MetronError
from ..models import StoryArc, StoryArcEntry


def register_arc_routes(app: FastAPI, templates: Jinja2Templates, get_db) -> None:
    def _client(db: Session):
        user, password, token = settings_store.metron_login(db)
        if not settings_store.metron_usable(user, password, token):
            raise HTTPException(400, "Save a Metron API token (or username and password) in Settings first.")
        return app.state.make_metron_client(user, password, token)

    def _arc(db: Session, arc_id: int) -> StoryArc:
        arc = db.get(StoryArc, arc_id)
        if arc is None:
            raise HTTPException(404)
        return arc

    @app.get("/arcs", response_class=HTMLResponse)
    async def arcs_page(request: Request, q: str = "", db: Session = Depends(get_db)):
        results, error = [], None
        if q.strip():
            try:
                async with _client(db) as client:
                    results = await client.search_arcs(q.strip())
            except HTTPException as exc:
                error = exc.detail
            except MetronError as exc:
                error = str(exc)
        followed = db.execute(
            select(StoryArc, func.count(StoryArcEntry.id))
            .outerjoin(StoryArcEntry)
            .group_by(StoryArc.id)
            .order_by(StoryArc.name)
        ).all()
        ours = {a.metron_id: a.id for a, _n in followed}
        return templates.TemplateResponse(
            request,
            "arcs.html",
            {"q": q, "results": results, "error": error, "followed": followed, "ours": ours},
        )

    @app.post("/arcs/add/{metron_id}")
    async def arc_add(metron_id: int, db: Session = Depends(get_db)):
        try:
            async with _client(db) as client:
                arc = await arcs.sync_arc(db, client, metron_id)
        except MetronError as exc:
            db.rollback()
            raise HTTPException(502, str(exc)) from exc
        log(db, f"Following the story arc '{arc.name}' ({len(arc.entries)} issues)", kind="add")
        db.commit()
        return RedirectResponse(f"/arcs/{arc.id}", status_code=303)

    @app.get("/arcs/{arc_id}", response_class=HTMLResponse)
    def arc_page(request: Request, arc_id: int, message: str = "", db: Session = Depends(get_db)):
        arc = _arc(db, arc_id)
        rs = arcs.rows(db, arc)
        return templates.TemplateResponse(
            request,
            "arc.html",
            {"arc": arc, "rows": rs, "sum": arcs.summary(rs), "missing": arcs.missing_series(db, arc),
             "message": message},
        )  # fmt: skip

    @app.post("/arcs/{arc_id}/refresh")
    async def arc_refresh(arc_id: int, db: Session = Depends(get_db)):
        arc = _arc(db, arc_id)
        try:
            async with _client(db) as client:
                await arcs.sync_arc(db, client, arc.metron_id)
        except MetronError as exc:
            db.rollback()
            raise HTTPException(502, str(exc)) from exc
        db.commit()
        return RedirectResponse(f"/arcs/{arc_id}", status_code=303)

    @app.post("/arcs/{arc_id}/monitor")
    def arc_monitor(arc_id: int, db: Session = Depends(get_db)):
        n = arcs.monitor_followed(db, _arc(db, arc_id))
        db.commit()
        return RedirectResponse(f"/arcs/{arc_id}?message=Monitored+{n}+issues", status_code=303)

    @app.post("/arcs/{arc_id}/add-series")
    async def arc_add_series(
        arc_id: int, series_ids: Annotated[list[int], Form()] = (), db: Session = Depends(get_db)
    ):
        arc = _arc(db, arc_id)
        wanted = {d["id"] for d in arcs.missing_series(db, arc)}
        added = 0
        try:
            async with _client(db) as client:
                for sid in dict.fromkeys(series_ids):
                    if sid not in wanted:
                        continue
                    # Nothing monitored but the arc's issues: the series stays followed for them.
                    await add_series(db, client, sid, option="none", monitor_new=False)
                    db.flush()
                    arcs.monitor_arc_issues_only(db, arc, sid)
                    added += 1
                    db.commit()
        except MetronError as exc:
            db.rollback()
            raise HTTPException(502, f"Added {added}, then Metron failed: {exc}") from exc
        return RedirectResponse(f"/arcs/{arc_id}?message=Added+{added}+series", status_code=303)

    @app.get("/arcs/{arc_id}/reading-list.cbl")
    def arc_cbl(arc_id: int, db: Session = Depends(get_db)):
        arc = _arc(db, arc_id)
        safe = "".join(c if c.isalnum() or c in " -_()" else "_" for c in arc.name).strip() or "arc"
        return Response(
            arcs.reading_list(db, arc),
            media_type="application/xml",
            headers={"Content-Disposition": f'attachment; filename="{safe}.cbl"'},
        )

    @app.post("/arcs/{arc_id}/delete")
    def arc_delete(arc_id: int, db: Session = Depends(get_db)):
        arc = _arc(db, arc_id)
        log(db, f"Stopped following the story arc '{arc.name}' (series and issues kept)", kind="series")
        db.delete(arc)
        db.commit()
        return RedirectResponse("/arcs", status_code=303)
