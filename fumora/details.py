"""Full issue records before an import (#24), so new files get full tags at once.

Tagging at import used to work from Fumora's own database only (series,
number, dates, rating). Credits, characters, story arcs and summaries live in
the issue's full record: Metron's for Metron series, ComicVine's for series
Metron doesn't have. DetailFetcher fetches those records just before a
download is imported.

It never holds up an import for long: Metron is skipped when the day's quota is
nearly spent (the daily refresh needs it) or while requests are held after a
rate-limit answer, ComicVine likewise while held, and the whole fetch stops
after DEADLINE. An issue that misses out is imported with basic tags; a tag
run fills in the rest later.
"""

import asyncio
import logging
import time
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from . import settings_store
from .comicvine import ComicVineAuthError, ComicVineError
from .importer import log
from .metron import MetronAuthError, MetronError, RateLimiter, _open
from .models import Issue
from .tagging import ensure_cv_detail, ensure_detail

METRON_RESERVE = 100  # requests of the day's Metron quota left for the refresh
DEADLINE = 300.0  # seconds one import may wait for records in all

logger = logging.getLogger("fumora.details")


class DetailFetcher:
    def __init__(
        self,
        make_metron_client: Callable,
        metron_limiter: RateLimiter,
        make_cv_client: Callable | None = None,
        cv_limiter: RateLimiter | None = None,
        clock: Callable[[], float] = time.time,
        deadline: float = DEADLINE,
    ):
        self._make_metron = make_metron_client
        self._metron_limiter = metron_limiter
        self._make_cv = make_cv_client
        self._cv_limiter = cv_limiter
        self._clock = clock
        self.deadline = deadline

    def metron_ok(self) -> bool:
        lim = self._metron_limiter
        if lim.blocked_until > self._clock():
            return False
        return lim.sustained_remaining is None or lim.sustained_remaining > METRON_RESERVE

    def cv_ok(self) -> bool:
        return self._cv_limiter is None or self._cv_limiter.blocked_until <= self._clock()

    async def fetch(self, session_factory, issue_ids: list[int]) -> dict[str, int]:
        """Fetch the missing records of these issues. Never raises."""
        counts = {"fetched": 0, "skipped": 0, "failed": 0}
        try:
            await asyncio.wait_for(self._fetch(session_factory, issue_ids, counts), self.deadline)
        except TimeoutError:
            counts["skipped"] += 1
            logger.info("issue records: stopped after %.0f s, the rest get basic tags", self.deadline)
        except Exception:  # an import must never fail over its tags
            counts["failed"] += 1
            logger.exception("issue records could not be fetched")
        return counts

    async def _fetch(self, session_factory, issue_ids: list[int], counts: dict[str, int]) -> None:
        with session_factory() as db:
            issues = db.scalars(
                select(Issue).where(Issue.id.in_(issue_ids)).options(selectinload(Issue.series))
            ).all()
            metron_todo = [i for i in issues if i.metron_id is not None and i.metron_detail is None]
            cv_todo = [i for i in issues if i.metron_id is None and i.cv_id and i.cv_detail is None]
            if metron_todo:
                user, password, token = settings_store.metron_login(db)
                if not settings_store.metron_usable(user, password, token):
                    counts["skipped"] += len(metron_todo)
                else:
                    async with _open(self._make_metron, user, password, token) as client:
                        for issue in metron_todo:
                            if not self.metron_ok():
                                counts["skipped"] += 1
                                continue
                            try:
                                await ensure_detail(db, client, issue)
                                counts["fetched"] += 1
                            except MetronAuthError:
                                counts["failed"] += 1
                                break
                            except MetronError as exc:
                                counts["failed"] += 1
                                log(
                                    db,
                                    f"No Metron record for {_label(issue)}: {exc}",
                                    kind="import",
                                    level="warning",
                                    issue_id=issue.id,
                                )
                            db.commit()
            key = settings_store.get(db, "comicvine.api_key")
            if cv_todo and key and self._make_cv is not None:
                async with self._make_cv(key) as cv:
                    for issue in cv_todo:
                        if not self.cv_ok():
                            counts["skipped"] += 1
                            continue
                        try:
                            await ensure_cv_detail(db, cv, issue)
                            counts["fetched"] += 1
                        except ComicVineAuthError:
                            counts["failed"] += 1
                            break
                        except ComicVineError as exc:  # includes rate limiting (then cv_ok is False)
                            counts["failed"] += 1
                            log(
                                db,
                                f"No ComicVine record for {_label(issue)}: {exc}",
                                kind="import",
                                level="warning",
                                issue_id=issue.id,
                            )
                        db.commit()
            elif cv_todo:
                counts["skipped"] += len(cv_todo)
            db.commit()


def _label(issue: Issue) -> str:
    return f"{issue.series.name} #{issue.number}"
