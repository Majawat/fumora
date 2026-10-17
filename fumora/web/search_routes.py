"""Interactive search for one issue (Sonarr-style): every result with its score
and the reasons it was or wasn't accepted. Grabbing comes in the next part."""

import asyncio

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import settings_store
from ..models import Issue
from ..prowlarr import ProwlarrClient, ProwlarrError
from ..search import evaluate, fallback_queries, load_profile, queries_for, rank


def register_search_routes(app: FastAPI, templates: Jinja2Templates, get_db) -> None:
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
        return templates.TemplateResponse(
            request,
            "_search_results.html",
            {
                "i": issue,
                "accepted": [c for c in ranked if c.accepted],
                "close": close,
                "hidden": len(ranked) - len(close) - sum(1 for c in ranked if c.accepted),
                "queries": queries,
                "error": error,
            },
        )
