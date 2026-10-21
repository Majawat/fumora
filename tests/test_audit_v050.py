"""Regressions for the independent (Gemini) audit of v0.5.0, one test per real finding."""

import os
import re
import shutil
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from fumora import naming, settings_store
from fumora.auto_search import make_search_handler
from fumora.ddl import _unpack
from fumora.grabbing import APPROVAL, ImportFailed, _place_converted, place_issue_file
from fumora.importer import unmatch_series
from fumora.library_jobs import RETAG, make_retag_handler, naming_settings
from fumora.metron import BASE_URL, MetronClient
from fumora.models import Event, File, Grab, Issue, Release, Series
from fumora.perms import FILE_MODE
from fumora.refresh import rolling_selection, update_releases
from fumora.sources import apply_cv_issues
from tests.helpers import make_cbz
from tests.test_downloads import SAB, _cbr_download, _release, _setup
from tests.test_m2_routes_jobs import _job
from tests.test_m3_refresh import client, page
from tests.test_m5b_sources import cv_issue
from tests.test_review_29_gemini import _rar_like

pytestmark = pytest.mark.skipif(shutil.which("bsdtar") is None, reason="bsdtar not installed")

FUMORA = Path(__file__).parents[1] / "fumora"


def _abs(db, number="17"):
    return db.scalar(select(Issue).where(Issue.number == number))


def _lib(app, tmp_path, numbers=("17",)):
    _setup(app, tmp_path, numbers=numbers)
    with app.state.session_factory() as db:
        settings_store.put(db, "import.tag", "off")
        db.commit()
    folder = tmp_path / "library" / "Absolute Batman (2024)"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_1_converting_a_library_file_in_place_recycles_the_original(app, tmp_path):
    folder = _lib(app, tmp_path)
    original = _rar_like(folder / "Absolute Batman 017 (2026).cbz", {"p1.jpg": b"\xff\xd8\xff" + b"x" * 300})
    raw = original.read_bytes()
    with app.state.session_factory() as db:
        issue = _abs(db)
        row, problem = place_issue_file(db, issue.series, issue, original, False, naming_settings(db))
        assert problem is None and row.path == str(original)
    assert zipfile.is_zipfile(original)  # converted where it was, not deleted
    recycled = list((tmp_path / "recycle").rglob("*.cbz"))
    assert len(recycled) == 1 and recycled[0].read_bytes() == raw  # the original is kept


def test_2_a_converted_download_obeys_the_same_mount_rule(tmp_path, monkeypatch):
    src = _cbr_download(tmp_path / "dl", "x.cbr")
    dest = tmp_path / "lib" / "x.cbr"
    dest.parent.mkdir()
    real_stat = Path.stat

    def fake_stat(self, *a, **k):
        st = real_stat(self, *a, **k)
        if self == src:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, *tuple(st)[3:]))
        return st

    monkeypatch.setattr(Path, "stat", fake_stat)
    with pytest.raises(ImportFailed, match="different mounts"):
        _place_converted(src, dest)


def test_3_organize_never_nests_a_series_deeper(app, tmp_path):
    lib = tmp_path / "lib"
    with app.state.session_factory() as db:
        from tests.test_scanner_naming import _series

        s = _series(db, lib / "Dark Horse" / "Umbrella Academy Dallas (2008)")
        plan = naming.plan_series(
            s, folder_template="{Publisher}/{Series Title} ({Series Year})", library_root=str(lib)
        )
        assert plan.folder is None  # already where the template puts it


def test_4_an_unlinked_row_at_the_destination_is_reused(app, tmp_path):
    folder = _lib(app, tmp_path)
    dest = folder / "Absolute Batman 017 (2026).cbz"
    with app.state.session_factory() as db:
        db.add(File(path=str(dest)))  # scanned earlier; the file was then removed outside Fumora
        db.commit()
        src = make_cbz(tmp_path / "Absolute Batman 017.cbz")
        issue = _abs(db)
        row, problem = place_issue_file(db, issue.series, issue, src, False, naming_settings(db))
        assert problem is None and row.path == str(dest)
        assert len(db.scalars(select(File)).all()) == 1


@pytest.mark.parametrize("status", ["review", "accepted", "pending"])
def test_5_a_series_awaiting_its_match_is_not_written(status):
    assert not Series(name="x", match_status=status).writable
    assert Series(name="x", match_status="not_found").writable
    assert not Series(name="x", match_status="matched").writable
    assert Series(name="x", match_status="matched", confirmed_by="auto").writable


def test_6_an_unreadable_pack_is_an_error_not_a_comic(tmp_path):
    from fumora.cbz import ArchiveError

    bad = tmp_path / "job" / "pack.zip"
    bad.parent.mkdir()
    bad.write_bytes(b"PK\x03\x04" + b"\x00" * 64)  # looks like a zip, isn't one
    with pytest.raises(ArchiveError):
        _unpack(bad)
    assert bad.exists() and not bad.with_suffix(".cbz").exists()


async def test_7_retag_leaves_hardlinked_torrent_files_alone(app, tmp_path):
    folder = _lib(app, tmp_path)
    seeding = make_cbz(tmp_path / "seeding.cbz")
    linked = folder / "Absolute Batman 017 (2026).cbz"
    os.link(seeding, linked)
    with app.state.session_factory() as db:
        settings_store.put(db, "metron.api_token", "t")
        _abs(db).files = [File(path=str(linked))]
        db.commit()
    msg = await make_retag_handler(app.state.make_metron_client)(_job(app, RETAG, {"dry_run": False}))
    assert "1 left alone (hardlinked" in msg
    assert os.stat(seeding).st_ino == os.stat(linked).st_ino


def test_8_converted_files_get_fumoras_permissions(tmp_path):
    from fumora.convert import convert_to_cbz

    src = _cbr_download(tmp_path / "x", "a.cbr")
    os.chmod(src, 0o600)
    assert (os.stat(convert_to_cbz(src, tmp_path / "recycle")).st_mode & 0o777) == FILE_MODE


def test_9_a_second_comicvine_issue_with_the_same_number_changes_nothing(app):
    with app.state.session_factory() as db:
        s = Series(name="Mieruko-chan", source="cv", cv_id=1, locked_fields=[])
        db.add(s)
        db.flush()
        apply_cv_issues(db, s, [cv_issue(1, "2020-01-01", cid=100)])
        apply_cv_issues(db, s, [cv_issue(1, "2021-06-01", cid=200) | {"name": "Variant"}])
        i = s.issues[0]
        assert (i.cv_id, i.title, i.cover_date) == (100, "Volume 1", date(2020, 1, 1))


def test_10_unmatching_keeps_a_locked_name(app):
    with app.state.session_factory() as db:
        s = Series(name="My Name", year_began=2001, mylar_name="Mylar Name", mylar_year=1999,
                   metron_id=5, match_status="matched", locked_fields=["name"])  # fmt: skip
        db.add(s)
        db.flush()
        unmatch_series(db, s, "wrong match")
        assert (s.name, s.year_began) == ("My Name", 1999)


def test_11_19_every_setting_the_code_reads_exists():
    """A key read with settings_store.get that isn't in SETTINGS can never be set:
    its feature silently does nothing (this was the case for two settings)."""
    keys = set()
    for path in FUMORA.rglob("*.py"):
        keys |= set(re.findall(r"settings_store\.get\(\s*db\s*,\s*\"([a-z_.]+)\"", path.read_text()))
    missing = sorted(k for k in keys if k not in settings_store.SETTINGS_BY_KEY)
    assert missing == [], f"settings read but never defined: {missing}"


def test_12_no_code_can_page_metrons_change_feed():
    assert not hasattr(MetronClient, "issues_modified_since")
    assert not hasattr(MetronClient, "series_modified_since")
    assert "modified_gt" not in (FUMORA / "metron.py").read_text()


@respx.mock
async def test_13_a_failing_series_goes_to_the_back_of_the_queue(app):
    from fumora.refresh import REFRESH, make_refresh_handler

    respx.get(url__regex=rf"{BASE_URL}issue/.*").respond(200, json=page([]))
    respx.get(f"{BASE_URL}series/7/").respond(404, json={"detail": "Not found."})
    with app.state.session_factory() as db:
        settings_store.put(db, "metron.api_token", "t")
        db.add(Series(name="Gone", metron_id=7, match_status="matched", confirmed_by="auto", monitored=True,
                      locked_fields=[]))  # fmt: skip
        db.commit()
    await make_refresh_handler(app.state.make_metron_client)(_job(app, REFRESH, {}))
    with app.state.session_factory() as db:
        gone = db.scalar(select(Series))
        assert gone.issues_synced is not None
        assert gone.id not in rolling_selection(db, 1, datetime.now(UTC))


async def test_14_a_missing_comicvine_issue_is_remembered(app):
    from fumora.tagging import ensure_cv_detail

    class NoCV:
        calls = 0

        async def issue(self, _id):
            NoCV.calls += 1
            return None

    with app.state.session_factory() as db:
        s = Series(name="x", source="cv", locked_fields=[])
        i = Issue(series=s, number="1", cv_id=9, locked_fields=[])
        db.add(s)
        db.flush()
        await ensure_cv_detail(db, NoCV(), i)
        await ensure_cv_detail(db, NoCV(), i)
        assert NoCV.calls == 1 and i.cv_detail == {}


@respx.mock
async def test_15_an_empty_answer_keeps_the_release_schedule(app):
    respx.get(f"{BASE_URL}issue/").mock(return_value=httpx.Response(200, json=page([])))
    with app.state.session_factory() as db:
        db.add(Release(metron_issue_id=99, metron_series_id=9, series_name="Kept", number="5",
                       store_date=date.today() + timedelta(days=3)))  # fmt: skip
        db.flush()
        async with client() as c:
            await update_releases(db, c)
        assert [r.series_name for r in db.scalars(select(Release))] == ["Kept"]


async def test_16_comicvine_series_retag_without_metron(app, tmp_path):
    folder = tmp_path / "cv"
    folder.mkdir()
    f = make_cbz(folder / "Mieruko-chan 001.cbz")
    with app.state.session_factory() as db:
        s = Series(name="Mieruko-chan", source="cv", match_status="not_found", path=str(folder),
                   locked_fields=[])  # fmt: skip
        s.issues = [Issue(number="1", locked_fields=[], files=[File(path=str(f))])]
        db.add(s)
        db.commit()
    msg = await make_retag_handler(app.state.make_metron_client)(_job(app, RETAG, {"dry_run": True}))
    assert msg.startswith("Preview:")


def test_17_add_search_without_a_login_shows_the_message(logged_in):
    r = logged_in.get("/add", params={"q": "batman"})
    assert r.status_code == 200 and "Metron API token" in r.text


def test_20_setup_needs_a_username(client):
    r = client.post("/setup", data={"username": "  ", "password": "longenough", "password2": "longenough"})
    assert r.status_code == 400 and "Enter a username." in r.text


@respx.mock
async def test_22_a_held_release_stays_held_if_its_replacement_fails(app, tmp_path):
    _setup(app, tmp_path, numbers=("16",))
    search = respx.get("http://prowlarr.test/api/v1/search")
    search.respond(200, json=[_release("Absolute Batman 016 (2026) FRENCH")])
    sab = respx.get(f"{SAB}/api", params={"mode": "addurl"})
    sab.respond(200, json={"status": True, "nzo_ids": ["n"]})
    await make_search_handler()(_job(app, "search", {}))
    search.respond(200, json=[_release("Absolute Batman 016 (2026) (Digital) (Shan-Empire)")])
    sab.respond(500)  # SABnzbd down: the better release can't be sent
    await make_search_handler()(_job(app, "search", {}))
    with app.state.session_factory() as db:
        states = {g.release_title: g.state for g in db.scalars(select(Grab))}
        assert states == {"Absolute Batman 016 (2026) FRENCH": APPROVAL}
        assert db.scalar(select(Event).where(Event.message.like("Couldn't send%"))) is not None
