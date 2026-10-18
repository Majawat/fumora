"""Interactive search for one issue (Sonarr-style): every result with its score
and the reasons it was or wasn't accepted, and a Grab button for each.

Results stay on the server under a short-lived token, so the page never carries
download links (they contain the Prowlarr API key)."""

import asyncio
import secrets
import time

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import settings_store
from ..auto_search import SEARCH
from ..downloaders import DownloaderError
from ..grabbing import active_grab, blocklist, grab, is_blocklisted
from ..importer import log
from ..models import Grab, Issue, Series
from ..prowlarr import ProwlarrClient, ProwlarrError
from ..search import evaluate, fallback_queries, load_profile, queries_for, rank

RESULTS_TTL = 30 * 60


def register_search_routes(app: FastAPI, templates: Jinja2Templates, get_db) -> None:
    app.state.search_results = {}  # token -> (expires, issue_id, [Candidate])

    def _remember(issue_id: int, candidates) -> str:
        now = time.time()
        for tok in [t for t, v in app.state.search_results.items() if v[0] < now]:
            del app.state.search_results[tok]
        token = secrets.token_urlsafe(12)
        app.state.search_results[token] = (now + RESULTS_TTL, issue_id, candidates)
        return token

    @app.get("/issues/{issue_id}/search", response_class=HTMLResponse)
    async def issue_search(request: Request, issue_id: int, db: Session = Depends(get_db)):
        issue = db.scalar(
            select(Issue)
            .where(Issue.id == issue_id)
            .options(selectinload(Issue.series), selectinload(Issue.files))
        )
        if issue is None:
            raise HTTPException(404)
        url = settings_store.get(db, "prowlarr.url")
        key = settings_store.get(db, "prowlarr.api_key")
        error, candidates, queries = None, [], queries_for(issue)
        if not url or not key:
            error = "Add your Prowlarr URL and API key in Settings first."
        else:
            profile = load_profile(db)
            seen: set[str] = set()

            def add(results: list[list[dict]]) -> None:
                for batch in results:
                    for rel in batch:
                        guid = rel.get("guid") or rel.get("downloadUrl")
                        if guid not in seen:
                            seen.add(guid)
                            candidates.append(evaluate(rel, issue, profile))

            try:
                async with ProwlarrClient(url, key) as prowlarr:
                    add(await asyncio.gather(*(prowlarr.search(q) for q in queries)))
                    if not any(c.accepted for c in candidates):
                        extra = fallback_queries(issue)
                        queries += extra
                        add(await asyncio.gather(*(prowlarr.search(q) for q in extra)))
            except ProwlarrError as exc:
                error = str(exc)
        ranked = rank(candidates)
        # Show what matters: everything accepted, plus rejected ones that were close
        # (right issue, filtered for size/words/seeders...).
        close = [c for c in ranked if not c.accepted and c.identified]
        accepted = [c for c in ranked if c.accepted]
        token = _remember(issue.id, accepted)
        for c in accepted:
            c.blocklisted = is_blocklisted(db, c.release)
        return templates.TemplateResponse(
            request,
            "_search_results.html",
            {
                "i": issue,
                "token": token,
                "busy": active_grab(db, issue.id),
                "accepted": accepted,
                "close": close,
                "hidden": len(ranked) - len(close) - sum(1 for c in ranked if c.accepted),
                "queries": queries,
                "error": error,
            },
        )

    @app.post("/issues/{issue_id}/grab")
    async def issue_grab(
        issue_id: int, token: str = Form(...), index: int = Form(...), db: Session = Depends(get_db)
    ):
        entry = app.state.search_results.get(token)
        if entry is None or entry[0] < time.time() or entry[1] != issue_id:
            raise HTTPException(410, "These search results expired; search again.")
        try:
            candidate = entry[2][index]
        except IndexError as exc:
            raise HTTPException(404) from exc
        issue = db.scalar(
            select(Issue)
            .where(Issue.id == issue_id)
            .options(selectinload(Issue.series).selectinload(Series.issues))
        )
        if active_grab(db, issue_id) is not None:
            raise HTTPException(409, "A download for this issue is already under way; see Queue.")
        if is_blocklisted(db, candidate.release):
            raise HTTPException(409, "That release is blocklisted.")
        try:
            await grab(db, candidate, issue)
            db.commit()
        except DownloaderError as exc:
            db.rollback()
            raise HTTPException(502, str(exc)) from exc
        return RedirectResponse("/queue", status_code=303)

    @app.post("/issues/{issue_id}/auto-search")
    async def issue_auto_search(issue_id: int):
        if app.state.jobs.active(SEARCH) is not None:
            raise HTTPException(409, "A search is already running; see Jobs.")
        app.state.jobs.start(SEARCH, {"issue_ids": [issue_id]})
        return RedirectResponse("/jobs", status_code=303)

    @app.post("/wanted/search")
    async def wanted_search():
        if app.state.jobs.active(SEARCH) is not None:
            raise HTTPException(409, "A search is already running; see Jobs.")
        app.state.jobs.start(SEARCH, {})
        return RedirectResponse("/jobs", status_code=303)

    def queue_context(db: Session) -> dict:
        grabs = db.scalars(select(Grab).order_by(Grab.id.desc()).limit(200)).all()
        issues = {
            i.id: i
            for i in db.scalars(
                select(Issue)
                .where(Issue.id.in_([g.issue_id for g in grabs if g.issue_id]))
                .options(selectinload(Issue.series))
            )
        }
        return {
            "active": [g for g in grabs if g.state in ("queued", "downloading", "importing")],
            "history": [g for g in grabs if g.state not in ("queued", "downloading", "importing")],
            "issues": issues,
        }

    @app.get("/queue", response_class=HTMLResponse)
    def queue_page(request: Request, db: Session = Depends(get_db)):
        return templates.TemplateResponse(request, "queue.html", queue_context(db))

    @app.get("/queue/table", response_class=HTMLResponse)
    def queue_table(request: Request, db: Session = Depends(get_db)):
        return templates.TemplateResponse(request, "_queue_table.html", queue_context(db))

    @app.post("/queue/{grab_id}/retry")
    async def queue_retry(grab_id: int, db: Session = Depends(get_db)):
        """Blocklist this release and search for another."""
        g = db.get(Grab, grab_id)
        if g is None:
            raise HTTPException(404)
        blocklist(db, g, "retried by hand")
        if g.state in ("queued", "downloading", "importing"):
            g.state = "removed"
        searching = app.state.jobs.active(SEARCH) is None and g.issue_id is not None
        log(
            db,
            f"Blocklisted '{g.release_title}'; "
            + ("searching again" if searching else "a search is already running, try again after it"),
            kind="grab",
            issue_id=g.issue_id,
        )
        db.commit()
        if searching:
            app.state.jobs.start(SEARCH, {"issue_ids": [g.issue_id]})
            return RedirectResponse("/jobs", status_code=303)
        return RedirectResponse("/queue", status_code=303)

    @app.post("/queue/{grab_id}/remove")
    def queue_remove(grab_id: int, db: Session = Depends(get_db)):
        """Stop following this download (it stays in the download client)."""
        g = db.get(Grab, grab_id)
        if g is None:
            raise HTTPException(404)
        g.state = "removed"
        db.commit()
        return RedirectResponse("/queue", status_code=303)
