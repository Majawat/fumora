"""Every configured search source behind one call: Prowlarr (usenet + torrents),
GetComics and Shelfmark (Anna's Archive). Each returns Prowlarr-shaped releases;
a source that fails is reported and the others still count."""

import asyncio
from collections.abc import Callable

from sqlalchemy.orm import Session

from . import settings_store
from .getcomics import GetComicsClient, GetComicsError
from .prowlarr import ProwlarrClient, ProwlarrError
from .search import _list
from .shelfmark import ShelfmarkClient, ShelfmarkError


class Indexers:
    def __init__(self, db: Session, transport=None):
        get = lambda k: settings_store.get(db, k)  # noqa: E731
        self.prowlarr = self.getcomics = self.shelfmark = None
        if get("prowlarr.url") and get("prowlarr.api_key"):
            self.prowlarr = ProwlarrClient(get("prowlarr.url"), get("prowlarr.api_key"), transport=transport)
        if get("getcomics.enabled") == "on":
            self.getcomics = GetComicsClient(
                get("getcomics.url"), get("flaresolverr.url"), transport=transport
            )
        if get("shelfmark.url"):
            self.shelfmark = ShelfmarkClient(
                get("shelfmark.url"), get("shelfmark.api_key"), transport=transport
            )
            self.shelfmark_sources = tuple(_list(get("shelfmark.sources"))) or ("direct_download",)

    @property
    def names(self) -> list[str]:
        return [
            n
            for n, c in (
                ("Prowlarr", self.prowlarr),
                ("GetComics", self.getcomics),
                ("Shelfmark", self.shelfmark),
            )
            if c is not None
        ]

    async def __aenter__(self) -> "Indexers":
        return self

    async def __aexit__(self, *exc) -> None:
        for c in (self.prowlarr, self.getcomics, self.shelfmark):
            if c is not None:
                await c.__aexit__(None, None, None)

    async def search(
        self, query: str, want: Callable[[str], bool], slow: bool = True
    ) -> tuple[list[dict], list[str]]:
        """Releases from every source, and one error line per source that failed.
        slow=False skips Anna's Archive (its searches can take minutes), for the
        issue-specific fallback searches."""
        jobs: list[tuple[str, object]] = []
        if self.prowlarr:
            jobs.append(("Prowlarr", self.prowlarr.search(query)))
        if self.getcomics:
            jobs.append(("GetComics", self.getcomics.search(query, want)))
        if self.shelfmark:
            # Anna's Archive searches can take minutes: only on the main searches.
            # Libgen answers in seconds, so it also takes the issue-specific ones.
            sources = (
                self.shelfmark_sources
                if slow
                else tuple(s for s in self.shelfmark_sources if s != "direct_download")
            )
            if sources:
                jobs.append(("Shelfmark", self.shelfmark.search(query, sources)))
        results = await asyncio.gather(*(j for _n, j in jobs), return_exceptions=True)
        releases, errors = [], []
        for (name, _j), res in zip(jobs, results, strict=True):
            if isinstance(res, ProwlarrError | GetComicsError | ShelfmarkError):
                errors.append(f"{name}: {res}")
            elif isinstance(res, BaseException):
                errors.append(f"{name}: {type(res).__name__}")
            else:
                releases += res
        return releases, errors
