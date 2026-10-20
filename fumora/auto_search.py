"""The 'search' job: find and grab releases for Wanted issues.

One search per series name (Prowlarr, GetComics, Shelfmark) covers all its
Wanted issues; an issue-specific search is the fallback (capped per run to spare
indexer APIs; it skips Shelfmark, whose Anna's Archive searches are slow).
Issues with a download already under way, blocklisted releases, and issues
inside the post-release delay are skipped.
"""

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from . import monitoring, settings_store
from .downloaders import DownloaderError
from .grabbing import APPROVAL, active_grab, grab, is_blocklisted
from .importer import log
from .indexers import Indexers
from .jobs import JobContext
from .models import Issue, Series
from .search import evaluate, fallback_queries, load_profile, queries_for, rank, wants

SEARCH = "search"
MAX_FALLBACK_SEARCHES = 20


def make_search_handler():
    async def run(ctx: JobContext) -> str:
        params = ctx.params()
        only = set(params.get("issue_ids") or [])
        # A retry after a failed download: limited to these issues, but otherwise an
        # ordinary search (monitored only, post-release delay applies).
        by_hand = bool(only) and not params.get("retry")
        sf = ctx.session_factory
        with sf() as db:
            try:
                delay = float(settings_store.get(db, "search.delay_hours") or 0)
            except ValueError:
                delay = 0.0
            indexers = Indexers(db)
            if not indexers.names:
                raise RuntimeError(
                    "No search source is set up (Prowlarr, GetComics or Shelfmark in Settings)"
                )
            q = (
                select(Issue)
                .join(Series)
                .options(selectinload(Issue.series).selectinload(Series.issues), selectinload(Issue.files))
            )
            if by_hand:
                # Asked for by hand: search them even if not monitored, as long as there's no file.
                q = q.where(Issue.id.in_(only), ~Issue.files.any())
            elif only:
                q = q.where(Issue.id.in_(only), monitoring.wanted_clause())
            else:
                q = q.where(monitoring.wanted_clause())
            wanted = db.scalars(q).all()
            cutoff = (datetime.now(UTC) - timedelta(hours=delay)).date()
            todo = [
                i
                for i in wanted
                if _searchable(active_grab(db, i.id))
                and (by_hand or delay <= 0 or (monitoring.release_date(i) or cutoff) <= cutoff)
            ]
            by_series: dict[int, list[Issue]] = defaultdict(list)
            for i in todo:
                by_series[i.series_id].append(i)
            ctx.update(phase="search", progress=0, total=len(by_series))
            grabbed, missing, errors, fallbacks, held = 0, 0, 0, 0, 0
            profile = load_profile(db)
            async with indexers:
                for n, (_sid, issues) in enumerate(by_series.items(), 1):
                    want = wants(issues)
                    results: list[dict] = []
                    failed: list[str] = []
                    answers = await asyncio.gather(
                        *(indexers.search(q, want) for q in queries_for(issues[0]))
                    )
                    for found, errs in answers:
                        results += found
                        failed += errs
                    # Every source failed every query (not just "nothing found"): an outage.
                    outage = all(len(errs) >= len(indexers.names) for _found, errs in answers)
                    if failed:
                        errors += len(failed)
                        log(
                            db,
                            f"Search problems for '{issues[0].series.name}': {'; '.join(failed)}",
                            kind=SEARCH,
                            level="error",
                        )
                        db.commit()
                        if outage:  # don't spend fallback searches on it
                            ctx.update(progress=n)
                            continue
                    taken: set[str] = set()
                    for issue in issues:
                        waiting = active_grab(db, issue.id)
                        if not _searchable(waiting):  # e.g. covered by a pack grabbed above
                            continue
                        pool = results
                        best = _best(pool, issue, profile, db, taken)
                        if (best is None or best.holdable) and fallbacks < MAX_FALLBACK_SEARCHES:
                            fallbacks += 1
                            for qtext in fallback_queries(issue):
                                found, _errs = await indexers.search(qtext, wants([issue]), slow=False)
                                pool = pool + found
                            best = _best(pool, issue, profile, db, taken)
                        if best is None:
                            missing += 1
                            continue
                        if waiting is not None:  # a release held for an OK
                            if best.holdable:
                                continue  # still nothing better: it keeps waiting
                            waiting.state = "removed"
                            waiting.message = f"Superseded by '{best.parsed.title}'"
                        try:
                            await grab(db, best, issue, approval=best.holdable)
                            taken.add(best.release.get("guid") or best.parsed.title)
                            if best.holdable:
                                held += 1
                            else:
                                grabbed += 1
                        except DownloaderError as exc:
                            errors += 1
                            log(db, f"Couldn't send '{best.parsed.title}': {exc}", kind=SEARCH, level="error")
                        db.commit()
                    ctx.update(progress=n)
            summary = f"Searched {len(todo)} wanted issues: grabbed {grabbed}, nothing found for {missing}"
            if held:
                summary += f", {held} waiting for your OK (Queue)"
            if errors:
                summary += f", {errors} errors"
            log(db, summary, kind=SEARCH)
            db.commit()
            return summary

    return run


def _searchable(g) -> bool:
    """No grab, or only a release waiting for an OK (a better one may turn up)."""
    return g is None or g.state == APPROVAL


def _best(results, issue, profile, db, taken):
    """The best accepted release, else the best one held only for an OK, else None."""
    ranked = rank([evaluate(r, issue, profile) for r in results])
    for c in ranked:
        if not (c.accepted or c.holdable):
            return None
        if (c.release.get("guid") or c.parsed.title) in taken or is_blocklisted(db, c.release):
            continue
        return c
    return None
