"""RSS sync (Sonarr/Mylar's RSS): every few minutes, the newest comics releases from
Prowlarr's indexers, matched against Wanted issues, so a new weekly is grabbed soon
after it is posted without searching for every Wanted issue.

Prowlarr gives an indexer's newest releases for a search with no query (its RSS). Each
release is looked at once (`rss.seen`, the newest few thousand guids); anything it
misses is still found by the regular Wanted search. Same rules as that search:
monitored, released, past the post-release delay, not blocklisted, quality profile,
held for an OK where the profile says so.
"""

import hashlib
import json
from collections import defaultdict

from . import settings_store
from .auto_search import SEARCH, _best, _searchable, searchable_wanted, send_best
from .grabbing import active_grab
from .importer import log
from .jobs import JobContext
from .prowlarr import ProwlarrClient
from .releases import parse_release, series_key
from .search import load_profile

RSS = "rss"
SEEN_KEY = "rss.seen"
MAX_SEEN = 5000


def _seen(db) -> list[str]:
    try:
        return list(json.loads(settings_store.get_state(db, SEEN_KEY) or "[]"))
    except ValueError:
        return []


def _id(r: dict) -> str:
    """A short fingerprint of a release (guids can be long URLs)."""
    return hashlib.sha1((r.get("guid") or r.get("title") or "").encode()).hexdigest()[:16]


def make_rss_handler(transport=None):
    async def run(ctx: JobContext) -> str:
        with ctx.session_factory() as db:
            url, key = settings_store.get(db, "prowlarr.url"), settings_store.get(db, "prowlarr.api_key")
            if not (url and key):
                raise RuntimeError("RSS sync needs Prowlarr (Settings)")
            ctx.update(phase="fetch")
            db.commit()
            async with ProwlarrClient(url, key, transport=transport) as prowlarr:
                releases = await prowlarr.search("")
            seen = _seen(db)
            known = set(seen)
            new = [r for r in releases if _id(r) not in known]
            seen = [_id(r) for r in new] + seen
            settings_store.put_state(db, SEEN_KEY, json.dumps(seen[:MAX_SEEN]))
            db.commit()
            if not new:
                return f"RSS: {len(releases)} releases, none new"
            try:
                delay = float(settings_store.get(db, "search.delay_hours") or 0)
            except ValueError:
                delay = 0.0
            todo = searchable_wanted(db, set(), False, delay)
            by_key: dict[str, set[int]] = defaultdict(set)
            for i in todo:
                for n in (i.series.name, i.series.mylar_name):
                    if n:
                        by_key[series_key(n)].add(i.series_id)
            pools: dict[int, list[dict]] = defaultdict(list)
            for r in new:
                sids = {s for k in parse_release(r.get("title") or "").series_keys for s in by_key.get(k, ())}
                for sid in sids:
                    pools[sid].append(r)
            profile = load_profile(db)
            grabbed = held = errors = 0
            taken: set[str] = set()
            candidates = [i for i in todo if i.series_id in pools]
            ctx.update(phase="match", progress=0, total=len(candidates))
            for n, issue in enumerate(candidates):
                ctx.update(progress=n + 1)
                waiting = active_grab(db, issue.id)
                if not _searchable(waiting):  # e.g. covered by a pack grabbed just now
                    continue
                best = _best(pools[issue.series_id], issue, profile, db, taken)
                if best is None:
                    continue
                outcome = await send_best(db, issue, best, waiting, taken)
                grabbed += outcome == "grabbed"
                held += outcome == "held"
                errors += outcome == "error"
            summary = f"RSS: {len(new)} new releases, {len(pools)} for wanted series: grabbed {grabbed}"
            if held:
                summary += f", {held} waiting for your OK (Queue)"
            if errors:
                summary += f", {errors} errors"
            if grabbed or held or errors:
                log(db, summary, kind=SEARCH)
            db.commit()
            return summary

    return run
