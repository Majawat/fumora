"""The 'search' job: find and grab releases for Wanted issues.

One Prowlarr search per series name covers all its Wanted issues; an
issue-specific search is the fallback (capped per run to spare indexer APIs).
Issues with a download already under way, blocklisted releases, and issues
inside the post-release delay are skipped.
"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from . import monitoring, settings_store
from .downloaders import DownloaderError
from .grabbing import active_grab, grab, is_blocklisted
from .importer import log
from .jobs import JobContext
from .models import Issue, Series
from .prowlarr import ProwlarrClient, ProwlarrError
from .search import evaluate, fallback_queries, load_profile, queries_for, rank

SEARCH = "search"
MAX_FALLBACK_SEARCHES = 20


def make_search_handler():
    async def run(ctx: JobContext) -> str:
        params = ctx.params()
        only = set(params.get("issue_ids") or [])
        sf = ctx.session_factory
        with sf() as db:
            url = settings_store.get(db, "prowlarr.url")
            key = settings_store.get(db, "prowlarr.api_key")
            try:
                delay = float(settings_store.get(db, "search.delay_hours") or 0)
            except ValueError:
                delay = 0.0
            if not url or not key:
                raise RuntimeError("Prowlarr isn't set up (Settings)")
            q = (
                select(Issue)
                .join(Series)
                .options(selectinload(Issue.series).selectinload(Series.issues), selectinload(Issue.files))
            )
            if only:
                # Asked for by hand: search them even if not monitored, as long as there's no file.
                q = q.where(Issue.id.in_(only), ~Issue.files.any())
            else:
                q = q.where(monitoring.wanted_clause())
            wanted = db.scalars(q).all()
            cutoff = (datetime.now(UTC) - timedelta(hours=delay)).date()
            todo = [
                i
                for i in wanted
                if active_grab(db, i.id) is None
                and (only or delay <= 0 or (monitoring.release_date(i) or cutoff) <= cutoff)
            ]
            by_series: dict[int, list[Issue]] = defaultdict(list)
            for i in todo:
                by_series[i.series_id].append(i)
            ctx.update(phase="search", progress=0, total=len(by_series))
            grabbed, missing, errors, fallbacks = 0, 0, 0, 0
            profile = load_profile(db)
            async with ProwlarrClient(url, key) as prowlarr:
                for n, (_sid, issues) in enumerate(by_series.items(), 1):
                    results: list[dict] = []
                    try:
                        for qtext in queries_for(issues[0]):
                            results += await prowlarr.search(qtext)
                    except ProwlarrError as exc:
                        errors += 1
                        log(
                            db,
                            f"Search failed for '{issues[0].series.name}': {exc}",
                            kind=SEARCH,
                            level="error",
                        )
                        db.commit()
                        ctx.update(progress=n)
                        continue
                    taken: set[str] = set()
                    for issue in issues:
                        if active_grab(db, issue.id) is not None:  # covered by a pack grabbed above
                            continue
                        pool = results
                        best = _best(pool, issue, profile, db, taken)
                        if best is None and fallbacks < MAX_FALLBACK_SEARCHES:
                            fallbacks += 1
                            try:
                                for qtext in fallback_queries(issue):
                                    pool = pool + await prowlarr.search(qtext)
                            except ProwlarrError:
                                pass
                            best = _best(pool, issue, profile, db, taken)
                        if best is None:
                            missing += 1
                            continue
                        try:
                            await grab(db, best, issue)
                            taken.add(best.release.get("guid") or best.parsed.title)
                            grabbed += 1
                        except DownloaderError as exc:
                            errors += 1
                            log(db, f"Couldn't send '{best.parsed.title}': {exc}", kind=SEARCH, level="error")
                        db.commit()
                    ctx.update(progress=n)
            summary = f"Searched {len(todo)} wanted issues: grabbed {grabbed}, nothing found for {missing}"
            if errors:
                summary += f", {errors} errors"
            log(db, summary, kind=SEARCH)
            db.commit()
            return summary

    return run


def _best(results, issue, profile, db, taken):
    ranked = rank([evaluate(r, issue, profile) for r in results])
    for c in ranked:
        if not c.accepted:
            return None
        if (c.release.get("guid") or c.parsed.title) in taken or is_blocklisted(db, c.release):
            continue
        return c
    return None
