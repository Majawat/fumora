import zipfile

import respx
from sqlalchemy import select

from fumora import settings_store
from fumora.jobs import JobContext
from fumora.library_jobs import make_organize_handler, make_retag_handler
from fumora.metron import BASE_URL, MetronClient, RateLimiter
from fumora.models import File, Issue, Job, Series, Tag
from tests.helpers import make_cbz, metron_issue
from tests.test_comicinfo import EXISTING


def _setup(app, tmp_path, confirmed="auto"):
    folder = tmp_path / "Private Dance (2023)"
    folder.mkdir()
    path = make_cbz(folder / "pd1.cbz", comicinfo=EXISTING)
    with app.state.session_factory() as db:
        settings_store.put(db, "metron.username", "u")
        settings_store.put(db, "metron.password", "p")
        s = Series(
            name="Private Dance",
            year_began=2023,
            metron_id=17753,
            match_status="matched",
            confirmed_by=confirmed,
            mylar_name="Private Dance",
            path=str(folder),
            locked_fields=[],
        )
        i = Issue(series=s, number="1", metron_id=186450, locked_fields=[])
        i.files = [File(path=str(path))]
        db.add_all([s, i])
        db.commit()
        return s.id, path


def _job(app, kind, params):
    with app.state.session_factory() as db:
        job = Job(kind=kind, params=params)
        db.add(job)
        db.commit()
        return JobContext(job.id, app.state.session_factory)


def test_confirm_edit_and_unmatch_routes(logged_in, app, tmp_path):
    sid, _ = _setup(app, tmp_path, confirmed=None)
    page = logged_in.get(f"/series/{sid}").text
    assert "Confirm this match" in page
    assert logged_in.post(f"/series/{sid}/retag", data={"write": "true"}).status_code == 400
    assert logged_in.post(f"/series/{sid}/organize").status_code == 400

    logged_in.post(f"/series/{sid}/confirm")
    logged_in.post(f"/series/{sid}/edit", data={"age_rating": "Adult", "tags": "Explicit, explicit, Romance"})
    with app.state.session_factory() as db:
        s = db.get(Series, sid)
        assert s.confirmed_by == "user"
        assert s.age_rating_override == "Adult"
        assert sorted(t.name for t in s.tags) == ["Explicit", "Romance"]
        assert len(db.scalars(select(Tag)).all()) == 2

    assert "Rename preview" in logged_in.get(f"/series/{sid}?rename=1").text

    logged_in.post(f"/series/{sid}/not-on-metron")
    with app.state.session_factory() as db:
        assert db.get(Series, sid).match_status == "not_found"


@respx.mock
async def test_retag_job_preview_then_write(app, tmp_path):
    sid, path = _setup(app, tmp_path)
    respx.get(f"{BASE_URL}issue/186450/").respond(200, json=metron_issue(186450))
    limiter = RateLimiter(min_interval=0)
    handler = make_retag_handler(lambda u, p: MetronClient(u, p, limiter))
    before = path.read_bytes()

    summary = await handler(_job(app, "retag", {"series_ids": [sid], "dry_run": True}))
    assert "1 files would change" in summary
    assert path.read_bytes() == before
    with app.state.session_factory() as db:
        issue = db.scalar(select(Issue))
        assert issue.metron_detail["id"] == 186450 and issue.rating == "Unknown"

    summary = await handler(_job(app, "retag", {"series_ids": [sid], "dry_run": False}))
    assert "1 files changed" in summary
    assert len(respx.calls) == 1  # Metron detail cached after the first run
    with zipfile.ZipFile(path) as zf:
        assert b"<Volume>2023</Volume>" in zf.read("ComicInfo.xml")


async def test_organize_job_renames(app, tmp_path):
    sid, path = _setup(app, tmp_path)
    summary = await make_organize_handler()(_job(app, "organize", {"series_ids": [sid]}))
    assert "Renamed 1" in summary
    assert not path.exists()
    assert (path.parent / "Private Dance 001 (2023).cbz").exists()


async def test_organize_skips_unconfirmed(app, tmp_path):
    sid, path = _setup(app, tmp_path, confirmed=None)
    summary = await make_organize_handler()(_job(app, "organize", {"series_ids": [sid]}))
    assert "skipped" in summary and path.exists()
