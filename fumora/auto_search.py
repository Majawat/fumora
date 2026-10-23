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
from .models import File, Grab, Issue, Series
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
            todo = searchable_wanted(db, only, by_hand, delay)
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
                    db.commit()  # end the read transaction before waiting on the network
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
                            db.commit()
                            for qtext in fallback_queries(issue):
                                found, errs = await indexers.search(qtext, wants([issue]), slow=False)
                                pool = pool + found
                                if errs:
                                    errors += len(errs)
                                    log(db, f"Search problems for '{issue.series.name} #{issue.number}': "
                                        f"{'; '.join(errs)}", kind=SEARCH, level="error")  # fmt: skip
                            best = _best(pool, issue, profile, db, taken)
                        if best is None:
                            missing += 1
                            continue
                        outcome = await send_best(db, issue, best, waiting, taken)
                        grabbed += outcome == "grabbed"
                        held += outcome == "held"
                        errors += outcome == "error"
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


def searchable_wanted(db, only: set[int], by_hand: bool, delay: float) -> list[Issue]:
    """Wanted issues a search may grab for now: monitored, released (past the delay), no
    file, no download under way (a release held for an OK may still be bettered), and no
    failed import waiting for a look. by_hand: the issues in `only`, monitored or not."""
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
    # A failed import needs a look (Queue: Try another blocklists it and searches
    # again); grabbing the same release automatically would fail the same way.
    stuck = {
        i
        for g in db.scalars(select(Grab).where(Grab.state == "import_failed"))
        for i in (g.issue_ids or [g.issue_id])
    }
    if not by_hand:  # asked for by hand: the owner has looked
        wanted = [i for i in wanted if i.id not in stuck]
    cutoff = (datetime.now(UTC) - timedelta(hours=delay)).date()
    todo = [
        i
        for i in wanted
        if _searchable(active_grab(db, i.id))
        and (by_hand or delay <= 0 or (monitoring.release_date(i) or cutoff) <= cutoff)
    ]
    return todo


async def send_best(db, issue: Issue, best, waiting: Grab | None, taken: set[str]) -> str:
    """Grab `best` for `issue` (held for an OK if it needs one). Returns grabbed, held,
    skipped (approved/removed or imported meanwhile, or nothing better than the held one)
    or error."""
    if waiting is not None:
        db.refresh(waiting)
        if waiting.state != APPROVAL:
            return "skipped"  # approved (or removed) by the owner meanwhile
    if waiting is not None and best.holdable:
        return "skipped"  # a release held for an OK, and still nothing better
    if db.scalar(select(File.id).where(File.issue_id == issue.id).limit(1)):
        return "skipped"  # imported while the search was waiting on indexers
    try:
        await grab(db, best, issue, approval=best.holdable)
        if waiting is not None:  # only once the better one was sent
            waiting.state = "removed"
            waiting.message = f"Superseded by '{best.parsed.title}'"
        taken.add(best.release.get("guid") or best.parsed.title)
        outcome = "held" if best.holdable else "grabbed"
    except DownloaderError as exc:
        log(db, f"Couldn't send '{best.parsed.title}': {exc}", kind=SEARCH, level="error")
        outcome = "error"
    db.commit()
    return outcome


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
