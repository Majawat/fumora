import httpx
import respx
from sqlalchemy import select

from fumora import settings_store
from fumora.import_job import make_handler
from fumora.importer import import_report, upsert_mylar
from fumora.jobs import JobContext
from fumora.metron import BASE_URL, MetronClient, RateLimiter
from fumora.models import File, Issue, Job, Series
from fumora.mylar import read_mylar
from tests.fake_mylar import build

LIB = "/data/media/Comics/Private Dance (2023)"


def page(results, next_url=None):
    return {"count": len(results), "next": next_url, "previous": None, "results": results}


def mylar_db(tmp_path):
    return build(
        tmp_path / "mylar.db",
        comics=[
            ("162288", "Private Dance", None, "2023", "Space Between", LIB, "Active", "Print", None),
            ("555", "Gone Series", None, "1990", "X", "/data/media/Comics/Gone", "Active", "Print", "17+"),
        ],
        issues=[
            ("1001", "162288", "1", None, "2023-09-01", None, "Downloaded", "Private Dance 001 (2023).cbz"),
            ("1002", "162288", "2", None, "2024-05-01", None, "Downloaded", "Private Dance 002 (2023).cbz"),
            ("1099", "162288", "99", None, "2024-05-01", None, "Skipped", None),
            ("2001", "555", "1", None, "1990-01-01", None, "Downloaded", "Gone 001.cbz"),
        ],
    )


def mock_metron():
    respx.get(f"{BASE_URL}series/", params={"cv_id": "162288"}).respond(
        200, json=page([{"id": 17753, "series": "Private Dance (2023)"}])
    )
    respx.get(f"{BASE_URL}series/", params={"cv_id": "555"}).respond(200, json=page([]))
    respx.get(f"{BASE_URL}series/", params={"name": "Gone Series"}).respond(200, json=page([]))
    respx.get(f"{BASE_URL}series/17753/").respond(
        200,
        json={
            "id": 17753,
            "name": "Private Dance",
            "sort_name": "Private Dance",
            "volume": 1,
            "year_began": 2023,
            "year_end": None,
            "series_type": {"id": 13, "name": "Single Issue"},
            "status": "Ongoing",
            "publisher": {"id": 261, "name": "Space Between Entertainment"},
            "imprint": None,
            "genres": [{"id": 1, "name": "Romance"}],
            "cv_id": 162288,
            "gcd_id": 205608,
            "desc": "",
            "modified": "2026-10-15T17:21:11-04:00",
        },
    )
    respx.get(f"{BASE_URL}issue/", params={"series_id": "17753"}).respond(
        200,
        json=page(
            [
                {"id": 186450, "number": "1", "cover_date": "2023-09-01", "store_date": None},
                {"id": 186451, "number": "2", "cover_date": "2024-05-01", "store_date": None},
                {"id": 186452, "number": "3", "cover_date": "2024-08-01", "store_date": None},
                {"id": 186459, "number": "10", "cover_date": "2099-03-01", "store_date": None},
            ]
        ),
    )


def existing(paths):
    return lambda p: p in paths


def test_upsert_is_idempotent_and_links_files(app, tmp_path):
    series = read_mylar(mylar_db(tmp_path))
    on_disk = existing({f"{LIB}/Private Dance 001 (2023).cbz"})
    with app.state.session_factory() as db:
        first = upsert_mylar(db, series, on_disk)
        db.commit()
        second = upsert_mylar(db, read_mylar(tmp_path / "mylar.db"), on_disk)
        db.commit()
        assert first.series_new == 2 and first.issues_new == 4
        assert first.files_linked == 1 and first.files_missing == 2
        assert second.series_new == 0 and second.issues_new == 0 and second.files_linked == 0
        gone = db.scalar(select(Series).where(Series.cv_id == 555))
        assert gone.age_rating_override == "Mature"


@respx.mock
async def test_full_import_job(app, tmp_path, monkeypatch):
    monkeypatch.setattr("fumora.importer.file_exists", lambda p: p.startswith(LIB))
    mock_metron()
    sf = app.state.session_factory
    with sf() as db:
        settings_store.put(db, "mylar.db_path", str(mylar_db(tmp_path)))
        settings_store.put(db, "metron.username", "u")
        settings_store.put(db, "metron.password", "p")
        job = Job(kind="mylar_import", params={})
        db.add(job)
        db.commit()
        job_id = job.id

    limiter = RateLimiter(min_interval=0)
    handler = make_handler(app.state.config.config_dir, lambda u, p: MetronClient(u, p, limiter))
    summary = await handler(JobContext(job_id, sf))
    assert "1/2 series matched" in summary

    with sf() as db:
        pd = db.scalar(select(Series).where(Series.cv_id == 162288))
        assert pd.metron_id == 17753 and pd.match_status == "matched"
        assert pd.publisher.name == "Space Between Entertainment"
        assert pd.genres == ["Romance"] and pd.issues_synced is not None
        issues = {i.number: i for i in pd.issues}
        assert issues["1"].metron_id == 186450 and issues["1"].match_status == "matched"
        assert issues["99"].match_status == "unmatched"
        # Only Metron has #3; it is already out and the series is monitored.
        assert issues["3"].match_status == "new" and issues["3"].status == "Wanted"
        # Only Metron has #10, and it is not released yet.
        assert issues["10"].match_status == "new" and issues["10"].status == "Skipped"
        assert (
            db.scalar(select(File).where(File.path == f"{LIB}/Private Dance 001 (2023).cbz")).issue_id
            == issues["1"].id
        )

        gone = db.scalar(select(Series).where(Series.cv_id == 555))
        assert gone.match_status == "not_found" and gone.metron_id is None

        r = import_report(db)
        assert r["series_matched"] == 1 and r["series_not_found"] == 1
        assert r["issues_new"] == 2 and r["issues_unmatched"] == 1

    # A second run makes no more Metron calls for already-matched series.
    calls_before = len(respx.calls)
    with sf() as db:
        job2 = Job(kind="mylar_import", params={})
        db.add(job2)
        db.commit()
        job2_id = job2.id
    await handler(JobContext(job2_id, sf))
    assert len(respx.calls) == calls_before
    with sf() as db:
        assert db.scalar(select(Issue).where(Issue.number == "10")) is not None
        assert len(db.scalars(select(Issue).where(Issue.number == "10")).all()) == 1


@respx.mock
async def test_duplicate_metron_series_goes_to_review(app, tmp_path):
    respx.get(f"{BASE_URL}series/").respond(200, json=page([{"id": 42, "series": "Dup"}]))
    respx.get(f"{BASE_URL}series/42/").respond(200, json={"id": 42, "name": "Dup"})
    from fumora.importer import match_series

    limiter = RateLimiter(min_interval=0)
    with app.state.session_factory() as db:
        a = Series(name="A", cv_id=1, locked_fields=[])
        b = Series(name="B", cv_id=2, locked_fields=[])
        db.add_all([a, b])
        db.flush()
        async with MetronClient("u", "p", limiter) as client:
            assert await match_series(db, client, a) == "matched"
            assert await match_series(db, client, b) == "review"
        assert "Same Metron series" in b.match_note


async def test_job_fails_cleanly_without_mylar_db(app, tmp_path):
    sf = app.state.session_factory
    with sf() as db:
        settings_store.put(db, "mylar.db_path", str(tmp_path / "nope.db"))
        settings_store.put(db, "metron.username", "u")
        settings_store.put(db, "metron.password", "p")
        db.commit()
    app.state.jobs.register("mylar_import", make_handler(tmp_path, lambda u, p: None))
    runner = app.state.jobs
    job_id = runner.start("mylar_import")
    import asyncio

    for _ in range(50):
        await asyncio.sleep(0.02)
        with sf() as db:
            job = db.get(Job, job_id)
            if job.state in ("done", "failed"):
                break
    assert job.state == "failed"
    assert "Mylar database not found" in job.message


def test_respx_unused_guard():
    assert httpx  # keep import for respx side effects


@respx.mock
async def test_name_and_year_suggestion_then_accept(app):
    from fumora.importer import match_series

    respx.get(f"{BASE_URL}series/", params={"cv_id": "28233"}).respond(200, json=page([]))
    respx.get(f"{BASE_URL}series/", params={"name": "The Umbrella Academy: Dallas"}).respond(
        200,
        json=page(
            [
                {"id": 8440, "series": "The Umbrella Academy: Dallas (2008)", "year_began": 2008},
                {"id": 8443, "series": "The Umbrella Academy TPB (2008)", "year_began": 2008},
            ]
        ),
    )
    respx.get(f"{BASE_URL}series/8440/").respond(
        200, json={"id": 8440, "name": "The Umbrella Academy: Dallas"}
    )
    limiter = RateLimiter(min_interval=0)
    with app.state.session_factory() as db:
        # Mylar/ComicVine says 2009, Metron 2008: within the one-year tolerance.
        s = Series(name="The Umbrella Academy: Dallas", year_began=2009, cv_id=28233, locked_fields=[])
        db.add(s)
        db.flush()
        async with MetronClient("u", "p", limiter) as client:
            assert await match_series(db, client, s) == "review"
            assert s.suggested_metron_id == 8440 and s.metron_id is None
            s.match_status = "accepted"
            assert await match_series(db, client, s) == "matched"
        assert s.metron_id == 8440 and s.suggested_metron_id is None
        assert "accepted" in s.match_note


@respx.mock
async def test_ambiguous_name_gives_no_suggestion(app):
    from fumora.importer import match_series

    respx.get(f"{BASE_URL}series/", params={"cv_id": "1"}).respond(200, json=page([]))
    respx.get(f"{BASE_URL}series/", params={"name": "Twin"}).respond(
        200,
        json=page(
            [
                {"id": 1, "series": "Twin (2000)", "year_began": 2000},
                {"id": 2, "series": "Twin (2000)", "year_began": 2000},
            ]
        ),
    )
    with app.state.session_factory() as db:
        s = Series(name="Twin", year_began=2000, cv_id=1, locked_fields=[])
        db.add(s)
        db.flush()
        async with MetronClient("u", "p", RateLimiter(min_interval=0)) as client:
            assert await match_series(db, client, s) == "not_found"
        assert s.suggested_metron_id is None
