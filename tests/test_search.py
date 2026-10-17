import json
from datetime import date

import respx

from fumora import settings_store
from fumora.models import Issue, Series
from fumora.search import Profile, evaluate, rank
from tests.helpers import FIXTURES

RELEASES = json.loads((FIXTURES / "prowlarr" / "absolute_batman.json").read_text())


def _issue(number, cover, series_type="Single Issue"):
    s = Series(name="Absolute Batman", year_began=2024, series_type=series_type)
    return Issue(series=s, number=number, cover_date=cover)


def _best(issue, profile=None):
    return rank([evaluate(r, issue, profile or Profile()) for r in RELEASES])


def test_picks_full_digital_usenet_release():
    ranked = _best(_issue("16", date(2026, 3, 1)))
    assert ranked[0].accepted
    assert ranked[0].parsed.title == "Absolute Batman 016 [2026] [Digital] [Shan-Empire]"
    accepted = [c.parsed.title for c in ranked if c.accepted]
    assert all("016" in t or "#16" in t for t in accepted)  # nothing for other issues


def test_mobile_ranks_below_full_digital():
    ranked = [c for c in _best(_issue("9", date(2025, 7, 1))) if c.accepted]
    titles = [c.parsed.title for c in ranked]
    assert titles.index("Absolute.Batman.009.2025.Digital.Shan-Empire") < titles.index(
        "Absolute.Batman.009.2025.digital-mobile.Son.of.Ultron-Empire"
    )


def test_lookalikes_are_rejected_with_reasons():
    ranked = _best(_issue("1", date(2024, 12, 1)))
    reasons = {c.parsed.title: c.rejections for c in ranked if not c.accepted}
    assert "not this issue" in reasons["Absolute.Batman.Noir.Edition.001.2025.digital.Son.of.Ultron-Empire"]
    assert any(r.startswith("ignored word: preview") for r in reasons[
        "Absolute.Batman.001.2024.All.In.Access.Preview.Edition.digital-Empire"])  # fmt: skip
    vol = next(t for t in reasons if t.startswith("Absolute Batman v01"))
    assert "a collected volume, not this issue" in reasons[vol]


def test_profile_controls():
    issue = _issue("16", date(2026, 3, 1))
    only_torrents = [c for c in _best(issue, Profile(protocols=["torrent"])) if c.accepted]
    assert only_torrents and all(c.protocol == "torrent" for c in only_torrents)
    blocked = _best(issue, Profile(blocked_groups=["Shan-Empire"]))
    assert blocked[0].parsed.group != "Shan-Empire"
    preferred = _best(issue, Profile(preferred_groups=["Kryptonian"]))
    assert "Kryptonian" in preferred[0].parsed.title
    assert not any(c.accepted for c in _best(issue, Profile(max_mb=10)))


def test_collected_series_takes_volumes():
    tpb = Series(name="Absolute Batman", year_began=2025, series_type="Trade Paperback")
    ranked = rank([evaluate(r, Issue(series=tpb, number="1"), Profile()) for r in RELEASES])
    accepted = [c.parsed for c in ranked if c.accepted]
    assert accepted and all(p.kind in ("volume", "issue") and p.number == "1" for p in accepted)


@respx.mock
def test_interactive_search_page(logged_in, app):
    with app.state.session_factory() as db:
        settings_store.put(db, "prowlarr.url", "http://prowlarr.test")
        settings_store.put(db, "prowlarr.api_key", "k")
        s = Series(name="Absolute Batman", year_began=2024, series_type="Single Issue", locked_fields=[])
        i = Issue(series=s, number="16", cover_date=date(2026, 3, 1), locked_fields=[])
        db.add_all([s, i])
        db.commit()
        iid = i.id
    respx.get("http://prowlarr.test/api/v1/search").respond(200, json=RELEASES)
    html = logged_in.get(f"/issues/{iid}/search").text
    assert "Absolute Batman 016 [2026] [Digital] [Shan-Empire]" in html
    assert "apikey" not in html
    assert respx.calls.last.request.headers["X-Api-Key"] == "k"
