"""#24: full tags at import (Metron's or ComicVine's issue record), and the version number."""

import re
import shutil
import tomllib
import zipfile
from datetime import date
from pathlib import Path

import pytest
import respx
from sqlalchemy import select

import fumora
from fumora import settings_store
from fumora.comicvine import BASE_URL as CV
from fumora.details import DetailFetcher
from fumora.grabbing import check_downloads, grab
from fumora.metron import BASE_URL as METRON
from fumora.models import Event, File, Issue, Series
from fumora.search import Profile, evaluate
from fumora.sources import apply_cv_issues
from fumora.tagging import cv_fields, issue_meta
from tests.helpers import make_cbz, metron_issue
from tests.test_downloads import SAB, _release, _setup
from tests.test_m5b_sources import cv_ok

pytestmark = pytest.mark.skipif(shutil.which("bsdtar") is None, reason="bsdtar not installed")

CV_RECORD = {
    "id": 9001,
    "name": "Volume 12",
    "description": "<p>Miko sees <b>things</b>.</p><p>Again &amp; again.</p>",
    "person_credits": [
        {"id": 1, "name": "Tomoki Izumi", "role": "writer, penciler, cover"},
        {"id": 2, "name": "Jan Mitsuko Cash", "role": "translator"},
        {"id": 3, "name": "Someone", "role": "production"},
    ],
    "character_credits": [{"id": 5, "name": "Miko Yotsuya"}, {"id": 6, "name": "Hana Yurikawa"}],
    "team_credits": [],
    "story_arc_credits": [{"id": 7, "name": "The Shrine"}],
    "date_last_updated": "2026-10-01 00:00:00",
    "site_detail_url": "https://comicvine.gamespot.com/mieruko-chan-12/4000-9001/",
}


def fetcher(app):
    app.state.comicvine_limiter.min_interval = 0
    app.state.comicvine_limiter.jitter = 0
    return DetailFetcher(
        app.state.make_metron_client,
        app.state.metron_limiter,
        app.state.make_cv_client,
        app.state.comicvine_limiter,
    )


def _sab_done(dl: Path):
    respx.get(f"{SAB}/api", params={"mode": "addurl"}).respond(200, json={"status": True, "nzo_ids": ["n1"]})
    respx.get(f"{SAB}/api", params={"mode": "queue"}).respond(200, json={"queue": {"slots": []}})
    respx.get(f"{SAB}/api", params={"mode": "history"}).respond(
        200, json={"history": {"slots": [{"nzo_id": "n1", "status": "Completed", "storage": str(dl)}]}}
    )


async def _grab_and_import(app, tmp_path, name, title, cv_series=False):
    """Grab issue #16 of the test series, let SABnzbd 'finish' it, run the monitor."""
    sf = app.state.session_factory
    dl = tmp_path / "usenet" / "fumora" / "job"
    dl.mkdir(parents=True)
    make_cbz(dl / name)
    _sab_done(dl)
    with sf() as db:
        issue = db.scalar(select(Issue).where(Issue.number == "16"))
        g = await grab(db, evaluate(_release(title), issue, Profile()), issue)
        db.commit()
        assert g.state in ("queued", "downloading"), g.message
    await check_downloads(sf, before_import=fetcher(app).fetch)
    with sf() as db:
        f = db.scalar(select(File))
        assert f is not None, [e.message for e in db.scalars(select(Event))]
        with zipfile.ZipFile(f.path) as zf:
            return zf.read("ComicInfo.xml").decode(), db.scalar(select(Issue).where(Issue.number == "16"))


def _metron_series(app, tmp_path):
    _setup(app, tmp_path)
    with app.state.session_factory() as db:
        settings_store.put(db, "metron.api_token", "tok")
        db.scalar(select(Issue).where(Issue.number == "16")).metron_id = 4743
        db.commit()


@respx.mock
async def test_metron_import_gets_credits_and_characters(app, tmp_path):
    _metron_series(app, tmp_path)
    route = respx.get(f"{METRON}issue/4743/").respond(200, json=metron_issue(4743))
    ci, issue = await _grab_and_import(app, tmp_path, "Absolute Batman 016 (2026).cbz", "Absolute Batman 016")
    assert route.call_count == 1
    assert "<Writer>Alan Moore</Writer>" in ci
    assert "Doctor Manhattan" in ci and "<Colorist>John Higgins</Colorist>" in ci
    assert issue.metron_detail is not None


@respx.mock
async def test_metron_quota_nearly_spent_imports_with_basic_tags(app, tmp_path):
    _metron_series(app, tmp_path)
    app.state.metron_limiter.sustained_remaining = 50  # the daily refresh needs these
    route = respx.get(f"{METRON}issue/4743/").respond(200, json=metron_issue(4743))
    ci, issue = await _grab_and_import(app, tmp_path, "Absolute Batman 016 (2026).cbz", "Absolute Batman 016")
    assert route.call_count == 0
    assert "<Series>Absolute Batman</Series>" in ci and "<Writer>" not in ci
    assert issue.metron_detail is None  # a tag run fetches it later


@respx.mock
async def test_metron_error_never_stops_the_import(app, tmp_path):
    _metron_series(app, tmp_path)
    respx.get(f"{METRON}issue/4743/").respond(404, json={"detail": "Not found."})
    ci, _ = await _grab_and_import(app, tmp_path, "Absolute Batman 016 (2026).cbz", "Absolute Batman 016")
    assert "<Series>Absolute Batman</Series>" in ci


@respx.mock
async def test_no_tagging_no_requests(app, tmp_path):
    _metron_series(app, tmp_path)
    with app.state.session_factory() as db:
        settings_store.put(db, "import.tag", "off")
        db.commit()
    route = respx.get(f"{METRON}issue/4743/").respond(200, json=metron_issue(4743))
    sf = app.state.session_factory
    dl = tmp_path / "usenet" / "fumora" / "job"
    dl.mkdir(parents=True)
    make_cbz(dl / "Absolute Batman 016 (2026).cbz")
    _sab_done(dl)
    with sf() as db:
        issue = db.scalar(select(Issue).where(Issue.number == "16"))
        await grab(db, evaluate(_release("Absolute Batman 016"), issue, Profile()), issue)
        db.commit()
    await check_downloads(sf, before_import=fetcher(app).fetch)
    assert route.call_count == 0


@respx.mock
async def test_comicvine_series_import_gets_comicvine_credits(app, tmp_path):
    _setup(app, tmp_path)
    with app.state.session_factory() as db:
        settings_store.put(db, "comicvine.api_key", "cvkey")
        s = db.scalar(select(Series))
        s.source, s.metron_id, s.match_status, s.cv_id = "cv", None, "not_found", 131898
        db.scalar(select(Issue).where(Issue.number == "16")).cv_id = 9001
        db.commit()
    route = respx.get(f"{CV}issue/4000-9001/").respond(200, json=cv_ok(CV_RECORD) | {"results": CV_RECORD})
    ci, issue = await _grab_and_import(app, tmp_path, "Absolute Batman 016 (2026).cbz", "Absolute Batman 016")
    assert route.call_count == 1 and route.calls[0].request.url.params["api_key"] == "cvkey"
    assert "<Writer>Tomoki Izumi</Writer>" in ci and "<CoverArtist>Tomoki Izumi</CoverArtist>" in ci
    assert "<Translator>Jan Mitsuko Cash</Translator>" in ci
    assert "Miko Yotsuya" in ci and "<StoryArc>The Shrine</StoryArc>" in ci
    summary = re.search(r"<Summary>(.*)</Summary>", ci, re.S).group(1)
    assert "<b>" not in summary and "&lt;" not in summary and "Again &amp; again." in summary
    assert issue.cv_detail["id"] == 9001
    assert "<Web>https://comicvine.gamespot.com/mieruko-chan-12/4000-9001/</Web>" in ci  # as Mylar wrote it


def test_cv_fields_map_roles_and_ignore_unknown_ones():
    f = cv_fields(CV_RECORD)
    assert f["credits"][0] == ("Tomoki Izumi", None, [("Writer", None), ("Penciller", None), ("Cover", None)])
    assert f["credits"][2] == ("Someone", None, [])
    assert f["summary"] == "Miko sees things.\nAgain & again."


def test_metron_record_wins_over_comicvine(app):
    with app.state.session_factory() as db:
        s = Series(name="X", locked_fields=[])
        i = Issue(
            series=s, number="1", locked_fields=[], metron_detail=metron_issue(4743), cv_detail=CV_RECORD
        )
        db.add(s)
        db.flush()
        meta = issue_meta(i)
    assert meta.credits[0][0] == "Alan Moore"
    assert all(c[0] != "Tomoki Izumi" for c in meta.credits)


def test_comicvine_change_drops_the_cached_record(app):
    with app.state.session_factory() as db:
        s = Series(name="Mieruko-chan", source="cv", cv_id=131898, locked_fields=[])
        i = Issue(series=s, number="12", cv_id=9001, locked_fields=[], cv_detail=CV_RECORD)
        db.add(s)
        db.flush()
        same = {"id": 9001, "issue_number": "12", "date_last_updated": CV_RECORD["date_last_updated"]}
        apply_cv_issues(db, s, [same])
        assert i.cv_detail is not None
        apply_cv_issues(db, s, [same | {"date_last_updated": "2026-10-19 00:00:00"}])
        assert i.cv_detail is None


async def test_deadline_stops_waiting(app, monkeypatch):
    import asyncio

    with app.state.session_factory() as db:
        s = Series(name="X", locked_fields=[])
        db.add(Issue(series=s, number="1", metron_id=1, locked_fields=[], cover_date=date(2026, 1, 1)))
        db.commit()
    f = fetcher(app)
    f.deadline = 0.05

    async def slow(*_a, **_k):
        await asyncio.sleep(5)

    monkeypatch.setattr(f, "_fetch", slow)
    counts = await f.fetch(app.state.session_factory, [1])
    assert counts["skipped"] == 1


def test_version_is_the_same_everywhere():
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == fumora.__version__
