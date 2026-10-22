"""Preferred languages (with an OK step), numberless graphic novels, Libgen titles."""

from datetime import date

import pytest
import respx
from sqlalchemy import select

from fumora import settings_store
from fumora.auto_search import make_search_handler
from fumora.grabbing import APPROVAL, active_grab
from fumora.models import Blocklist, Grab, Issue, Series
from fumora.releases import matches_issue, parse_release
from fumora.search import Profile, evaluate, language_code, load_profile, rank
from fumora.shelfmark import clean_libgen_title, to_release
from tests.test_ddl_sources import SM, _gc_settings
from tests.test_downloads import SAB, _issue, _release, _setup
from tests.test_m2_routes_jobs import _job

# --- languages ---------------------------------------------------------------------


def test_language_codes():
    assert [language_code(x) for x in ("en", "EN", "English", "eng", "pol", "fre", "Croatian")] == [
        "en", "en", "en", "en", "pl", "fr", "hr",
    ]  # fmt: skip


def test_other_language_is_held_not_rejected(app, tmp_path):
    _setup(app, tmp_path, numbers=("16",))
    with app.state.session_factory() as db:
        issue = _issue(db, "16")
        french = evaluate(_release("Absolute Batman 016 (2026) FRENCH"), issue, Profile())
        assert not french.accepted and french.holdable and french.hold == "language: fr"
        lg = {**_release("Absolute Batman 016 (2026)"), "language": "pl"}
        assert evaluate(lg, issue, Profile()).hold == "language: pl"
        assert evaluate(lg, issue, Profile(other_languages="reject")).rejections == ["language: pl"]
        assert evaluate(lg, issue, Profile(languages=["en", "pl"])).accepted
        english = evaluate(_release("Absolute Batman 016 (2026)"), issue, Profile())
        assert [c.hold for c in rank([french, english])] == [None, "language: fr"]


def test_language_settings_are_read(app):
    with app.state.session_factory() as db:
        settings_store.put(db, "search.languages", "en, French")
        settings_store.put(db, "search.other_languages", "reject")
        p = load_profile(db)
    assert p.languages == ["en", "fr"] and p.other_languages == "reject"


@respx.mock
async def test_auto_search_holds_a_foreign_release_for_an_ok(app, tmp_path, logged_in):
    _setup(app, tmp_path, numbers=("16",))
    respx.get("http://prowlarr.test/api/v1/search").respond(
        200, json=[_release("Absolute Batman 016 (2026) FRENCH")]
    )
    add = respx.get(f"{SAB}/api", params={"mode": "addurl"}).respond(
        200, json={"status": True, "nzo_ids": ["nzo_9"]}
    )
    summary = await make_search_handler()(_job(app, "search", {}))
    assert "1 waiting for your OK" in summary and not add.called
    with app.state.session_factory() as db:
        g = db.scalar(select(Grab))
        assert g.state == APPROVAL and "language: fr" in g.message
        assert active_grab(db, _issue(db, "16").id) is not None  # no second hold next run
    page = logged_in.get("/queue").text
    assert "Needs your OK" in page and "OK, download" in page

    r = logged_in.post(f"/queue/{g.id}/approve", follow_redirects=False)
    assert r.status_code == 303 and add.called
    with app.state.session_factory() as db:
        g = db.get(Grab, g.id)
        assert (g.state, g.client_job_id) == ("downloading", "nzo_9")


def test_reject_blocklists_the_held_release(app, tmp_path, logged_in):
    _setup(app, tmp_path, numbers=("16",))
    with app.state.session_factory() as db:
        g = Grab(issue_id=_issue(db, "16").id, issue_ids=[], indexer="x", release_title="AB 016 FRENCH",
                 guid="g1", protocol="usenet", client="sabnzbd", state=APPROVAL)  # fmt: skip
        db.add(g)
        db.commit()
        gid = g.id
    assert logged_in.post(f"/queue/{gid}/reject", follow_redirects=False).status_code == 303
    with app.state.session_factory() as db:
        assert db.get(Grab, gid).state == "removed"
        assert db.scalar(select(Blocklist.guid)) == "g1"
    assert logged_in.post(f"/queue/{gid}/approve").status_code == 404  # no longer waiting


@respx.mock
def test_issue_search_lists_other_languages_with_grab_anyway(app, tmp_path, logged_in):
    _setup(app, tmp_path, numbers=("16",))
    respx.get("http://prowlarr.test/api/v1/search").respond(
        200, json=[_release("Absolute Batman 016 (2026) FRENCH")]
    )
    with app.state.session_factory() as db:
        iid = _issue(db, "16").id
    page = logged_in.get(f"/issues/{iid}/search").text
    assert "Other languages (1)" in page and "Grab anyway" in page


# --- numberless graphic novels -------------------------------------------------------


@pytest.fixture
def blankets(app):
    with app.state.session_factory() as db:
        s = Series(name="Blankets", year_began=2003, series_type="Graphic Novel", match_status="matched",
                   locked_fields=[], monitored=True)  # fmt: skip
        db.add(Issue(series=s, number="1", cover_date=date(2003, 7, 1), locked_fields=[], monitored=True))
        db.add(s)
        db.commit()
    return app


@pytest.mark.parametrize(
    ("title", "ok"),
    [
        ("Blankets (2003).cbz", True),  # archive format + year
        ("Blankets.cbr", True),  # archive format, no year
        ("Blankets (2004) (Digital)", True),  # usenet title: year matches
        ("Blankets.epub", False),  # a book
        ("Blankets.pdf", False),  # could be anything: no year, no comic format
        ("Blankets (1987).cbz", False),  # another work of that name
        ("Blankets Of Snow (2003).cbz", False),  # another name
    ],
)
def test_graphic_novel_without_a_number(blankets, title, ok):
    with blankets.state.session_factory() as db:
        issue = db.scalar(select(Issue).join(Series).where(Series.name == "Blankets"))
        c = evaluate({"title": title, "size": 80 * 2**20, "protocol": "usenet"}, issue, Profile())
    assert c.accepted is ok, c.rejections


def test_numberless_title_never_counts_for_a_multi_issue_series(app, tmp_path):
    _setup(app, tmp_path, numbers=("1", "2"))
    with app.state.session_factory() as db:
        c = evaluate(_release("Absolute Batman (2026).cbz"), _issue(db, "1"), Profile())
    assert not c.accepted


# --- Libgen titles ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Black Hole #1 1995-mar 1 Biology 101 c c 1013165 John Burns - Black Hole T1 (en) "
         "1024x1569px 300dpi",
         "Black Hole #1 (1995) (en)"),
        ("Sweet Tooth #4 c c 1154288 Sweet Tooth 004 (2010) (digital) (Minutemen-Excelsior)",
         "Sweet Tooth #4 (2010) (digital) (Minutemen-Excelsior)"),
        ("Craig Thompson's Blankets c c 1034422 Blankets 1000x1571px 150dpi", "Craig Thompson's Blankets"),
        ("Absolute Batman 024 (2026) (Digital)", "Absolute Batman 024 (2026) (Digital)"),
    ],
)  # fmt: skip
def test_clean_libgen_title(raw, clean):
    assert clean_libgen_title(raw) == clean


def test_libgen_release_uses_clean_title_and_language(app, tmp_path):
    _setup(app, tmp_path, numbers=("1",))
    raw = {"source": "libgen", "source_id": "libgen:abc", "format": "cbr", "size": "10 MB", "language": "fr",
           "title": "Absolute Batman #1 2024-oct 1 The Zoo c c 99 Absolute Batman 01 (2024) (Digital) "
                    "900x1400px"}  # fmt: skip
    rel = to_release(raw)
    assert rel["title"] == "Absolute Batman #1 (2024) (Digital).cbr" and rel["indexer"] == "Libgen"
    with app.state.session_factory() as db:
        c = evaluate(rel, _issue(db, "1"), Profile())
    assert c.identified and c.hold == "language: fr"


@respx.mock
async def test_libgen_runs_in_fallback_searches_but_annas_archive_does_not(app, tmp_path):
    from fumora.indexers import Indexers
    from fumora.search import wants

    _setup(app, tmp_path, numbers=("16",))
    _gc_settings(app, tmp_path)
    with app.state.session_factory() as db:
        settings_store.put(db, "shelfmark.url", SM)
        settings_store.put(db, "shelfmark.sources", "direct_download, libgen")
        settings_store.put(db, "getcomics.enabled", "off")
        db.commit()
        respx.get("http://prowlarr.test/api/v1/search").respond(200, json=[])
        route = respx.get(f"{SM}/api/releases").respond(200, json={"releases": []})
        async with Indexers(db) as idx:
            await idx.search("Absolute Batman 16", wants([_issue(db, "16")]), slow=False)
    assert [c.request.url.params["source"] for c in route.calls] == ["libgen"]


# --- failed downloads search again ---------------------------------------------------------


@respx.mock
async def test_failed_download_asks_for_a_new_search(app, tmp_path):
    import httpx

    from fumora.grabbing import MAX_RETRIES_PER_DAY, check_downloads

    _setup(app, tmp_path, numbers=("16",))
    with app.state.session_factory() as db:
        settings_store.put(db, "shelfmark.url", SM)
        iid = _issue(db, "16").id
        for n in range(MAX_RETRIES_PER_DAY + 1):
            db.add(Grab(issue_id=iid, issue_ids=[iid], indexer="Libgen", release_title=f"AB 016 copy {n}",
                        guid=f"libgen:{n}", protocol="ddl", client="shelfmark", client_job_id=f"libgen:{n}",
                        state="downloading"))  # fmt: skip
        db.commit()
    respx.get(f"{SM}/api/status").mock(
        return_value=httpx.Response(
            200, json={"error": {f"libgen:{n}": {"status_message": "dead"} for n in range(9)}}
        )
    )
    asked = []
    await check_downloads(app.state.session_factory, on_failed=asked.append)
    assert asked == [[iid]]
    with app.state.session_factory() as db:
        assert db.scalar(select(Blocklist.id).where(Blocklist.guid == "libgen:0")) is not None
        # a sixth failure today: no more automatic searches
        db.add(Grab(issue_id=iid, issue_ids=[iid], indexer="Libgen", release_title="AB 016 copy 9",
                    guid="libgen:9", protocol="ddl", client="shelfmark", client_job_id="libgen:8",
                    state="downloading"))  # fmt: skip
        db.commit()
    asked.clear()
    await check_downloads(app.state.session_factory, on_failed=asked.append)
    assert asked == []


async def test_retry_can_be_turned_off(app, tmp_path):
    from fumora.grabbing import retry_candidates

    _setup(app, tmp_path, numbers=("16",))
    with app.state.session_factory() as db:
        g = Grab(issue_id=_issue(db, "16").id, issue_ids=[], indexer="x", release_title="r", protocol="ddl",
                 client="shelfmark", state="failed")  # fmt: skip
        db.add(g)
        db.commit()
        assert retry_candidates(db, g) == [g.issue_id]
        settings_store.put(db, "search.retry_failed", "off")
        assert retry_candidates(db, g) == []


# --- review regressions -------------------------------------------------------------------


@respx.mock
async def test_held_release_is_superseded_when_a_preferred_one_appears(app, tmp_path, logged_in):
    _setup(app, tmp_path, numbers=("16",))
    search = respx.get("http://prowlarr.test/api/v1/search")
    search.respond(200, json=[_release("Absolute Batman 016 (2026) FRENCH")])
    respx.get(f"{SAB}/api", params={"mode": "addurl"}).respond(200, json={"status": True, "nzo_ids": ["n"]})
    await make_search_handler()(_job(app, "search", {}))
    await make_search_handler()(_job(app, "search", {}))  # still only French: no second hold
    with app.state.session_factory() as db:
        assert [g.state for g in db.scalars(select(Grab))] == [APPROVAL]
    search.respond(200, json=[_release("Absolute Batman 016 (2026) (Digital) (Shan-Empire)")])
    summary = await make_search_handler()(_job(app, "search", {}))
    assert "grabbed 1" in summary
    with app.state.session_factory() as db:
        states = {g.release_title: g.state for g in db.scalars(select(Grab))}
    assert states["Absolute Batman 016 (2026) FRENCH"] == "removed"
    assert states["Absolute Batman 016 (2026) (Digital) (Shan-Empire)"] == "downloading"


@respx.mock
async def test_retry_search_respects_monitoring(app, tmp_path):
    _setup(app, tmp_path, numbers=("16",))
    with app.state.session_factory() as db:
        _issue(db, "16").monitored = False
        db.commit()
        iid = _issue(db, "16").id
    search = respx.get("http://prowlarr.test/api/v1/search").respond(200, json=[])
    summary = await make_search_handler()(_job(app, "search", {"issue_ids": [iid], "retry": True}))
    assert summary.startswith("Searched 0 wanted issues") and not search.called
    await make_search_handler()(_job(app, "search", {"issue_ids": [iid]}))  # by hand: searched
    assert search.called


def test_failed_pack_retries_every_issue_it_covered(app, tmp_path):
    from fumora.grabbing import retry_candidates

    _setup(app, tmp_path, numbers=("20", "21", "22"))
    with app.state.session_factory() as db:
        ids = [_issue(db, n).id for n in ("20", "21", "22")]
        g = Grab(issue_id=ids[0], issue_ids=ids, indexer="GetComics", release_title="AB #20-22",
                 protocol="ddl", client="getcomics", state="failed")  # fmt: skip
        db.add(g)
        db.commit()
        assert retry_candidates(db, g) == ids


@respx.mock
async def test_one_source_failing_with_no_results_still_runs_fallbacks(app, tmp_path):
    _setup(app, tmp_path, numbers=("16",))
    with app.state.session_factory() as db:
        settings_store.put(db, "shelfmark.url", SM)
        settings_store.put(db, "shelfmark.sources", "libgen")
        db.commit()
    prowlarr = respx.get("http://prowlarr.test/api/v1/search").respond(200, json=[])
    respx.get(f"{SM}/api/releases").respond(500, json={"error": "boom"})
    await make_search_handler()(_job(app, "search", {}))
    queries = [c.request.url.params["query"] for c in prowlarr.calls]
    assert "Absolute Batman 016" in queries  # the issue-specific fallback ran


def test_new_install_logs_its_random_refresh_time(app, client):
    from fumora.models import Event

    with app.state.session_factory() as db:
        events = [e.message for e in db.scalars(select(Event))]
    assert any("Daily Metron refresh set to" in m for m in events)


# --- manga: volumes listed as issues ---------------------------------------------------------


def _manga(db, numbering="auto", titles=True):
    s = Series(name="Mieruko-chan", year_began=2020, match_status="not_found", locked_fields=[],
               monitored=True, numbering=numbering)  # fmt: skip
    for n in range(1, 13):
        db.add(Issue(series=s, number=str(n), title=f"Volume {n}" if titles else None,
                     cover_date=date(2020 + n // 3, 1, 1), locked_fields=[], monitored=True))  # fmt: skip
    db.add(s)
    db.commit()
    return db.scalar(select(Issue).where(Issue.series_id == s.id, Issue.number == "12"))


@pytest.mark.parametrize(
    ("title", "size", "ok"),
    [
        ("Yen.Press-Mieruko.Chan.Vol.12.2026.Retail.Comic", 439, True),  # big: collected limit applies
        ("Mieruko-Chan, Vol. 12 by Tomoki Izumi [ENG / PDF]", 439, True),
        ("Mieruko-chan v12 (2026) (Digital) (1r0n)", 90, True),
        ("Yen.Press.-.Mieruko.Chan.Vol.12.2026.Retail.Comic.eBook-BitBook.cbz", 439, True),  # Libgen
        ("Mieruko-chan Vol. 11 by Tomoki Izumi [ENG / CBZ]", 80, False),  # another volume
        ("Mieruko-chan Vol. 12 by Tomoki Izumi [ENG / EPUB]", 80, False),  # ebook
    ],
)
def test_manga_volumes_match_issue_numbers(app, title, size, ok):
    with app.state.session_factory() as db:
        issue = _manga(db)
        assert issue.series.issues_are_volumes
        c = evaluate({"title": title, "size": size * 2**20, "protocol": "usenet"}, issue, Profile())
    assert c.accepted is ok, c.rejections


def test_numbering_can_be_set_by_hand(app, logged_in):
    with app.state.session_factory() as db:
        issue = _manga(db, titles=False)
        sid = issue.series_id
        assert not issue.series.issues_are_volumes  # no "Volume N" titles: issue numbering
    logged_in.post(f"/series/{sid}/edit", data={"numbering": "volumes"})
    with app.state.session_factory() as db:
        s = db.get(Series, sid)
        assert s.numbering == "volumes" and s.issues_are_volumes
        c = evaluate(_release("Mieruko-chan v12 (2026) (Digital)"), _issue(db, "12"), Profile())
    assert c.accepted


def test_libgen_folder_paths_are_dropped():
    raw = r"Yen.Press.-.Mieruko.Chan.Vol.12.2026.Retail.Comic.eBook-BitBook s:\scene\2026-04\Yen"
    assert clean_libgen_title(raw) == "Yen.Press.-.Mieruko.Chan.Vol.12.2026.Retail.Comic.eBook-BitBook"


# --- #7: what the live Shelfmark test (2026-10-22) found ------------------------------


@pytest.mark.parametrize(
    ("raw", "series", "number"),
    [
        ("Walking Dead (The) 1 #100 (Image) (2012) (Digital) (Zone-Empire).cbr", "Walking Dead", "100"),
        ("Superman: Batman 1 #14 (2005) (Digital) (Zone-Empire).cbr", "Superman: Batman", "14"),
        ("Black Hole #1 (1995).cbr", "Black Hole", "1"),
    ],
)
def test_libgen_volume_before_the_issue_is_not_part_of_the_name(raw, series, number):
    r = parse_release(clean_libgen_title(raw))
    assert (r.series, r.kind, r.number) == (series, "issue", number)
    assert matches_issue(
        r, ["The Walking Dead", "Superman/Batman", "Superman: Batman", "Black Hole"], number, ()
    )


def test_a_cover_only_file_is_not_the_issue():
    s = Series(name="The Walking Dead", year_began=2003, series_type="Single Issue")
    issue = Issue(series=s, number="100")
    title = "Walking Dead (The) v1 #100 (2012) (Cover B - Marc Silvestri Variant) (Cover ONLY) (ScanDog).cbr"
    c = evaluate({"title": title, "size": 8 * 2**20, "protocol": "ddl"}, issue, Profile())
    assert "only the cover" in c.rejections


def test_language_from_a_bracket_tag_but_not_from_a_name():
    assert parse_release("Walking Dead (The) v1 #100 (rus).cbz").language == "ru"
    assert parse_release("Batman Poche #50 (1984) (Comics Fr).cbr").language == "fr"
    assert parse_release("The Walking Dead v01-v32 by Tony Moore, Rus Wooton [ENG / CBR]").language == "en"
