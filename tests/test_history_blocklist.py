"""History page (series/issue activity) and the Blocklist page."""

from sqlalchemy import func, select

from fumora.grabbing import blocklist
from fumora.importer import log
from fumora.models import Blocklist, Event, Grab, Issue, Series


def _data(app):
    with app.state.session_factory() as db:
        s = Series(name="Saga", year_began=2012, locked_fields=[])
        other = Series(name="Paper Girls", year_began=2015, locked_fields=[])
        i = Issue(series=s, number="7", locked_fields=[])
        db.add_all([s, other, i])
        db.flush()
        log(db, "Grabbed 'Saga 007'", kind="grab", issue_id=i.id)  # series filled in from the issue
        log(db, "Download failed: 'Saga 007'", kind="grab", level="error", issue_id=i.id)
        log(db, "Imported Paper Girls 001", kind="import", series_id=other.id)
        g = Grab(issue_id=i.id, issue_ids=[i.id], release_title="Saga 007 (bad)", indexer="x",
                 client="sabnzbd", guid="g1")  # fmt: skip
        db.add(g)
        db.flush()
        blocklist(db, g, "download failed")
        db.commit()
        return s.id, i.id, other.id


def test_issue_events_belong_to_their_series(app):
    sid, iid, _ = _data(app)
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count(Event.id)).where(Event.series_id == sid)) == 2
        b = db.scalar(select(Blocklist))
        assert (b.series_id, b.issue_id) == (sid, iid)


def test_history_page_filters(app, logged_in):
    sid, iid, oid = _data(app)
    page = logged_in.get("/history").text
    assert "Grabbed &#39;Saga 007&#39;" in page and "Imported Paper Girls 001" in page
    assert f'href="/issues/{iid}">#7' in page
    only = logged_in.get(f"/history?series_id={sid}").text
    assert "Saga 007" in only and "Paper Girls 001" not in only
    errors = logged_in.get("/history?level=error").text
    assert "Download failed" in errors and "Grabbed &#39;Saga" not in errors
    assert "Imported Paper Girls" in logged_in.get("/history?kind=import").text
    assert "Download failed" in logged_in.get(f"/series/{sid}").text
    assert "Download failed" in logged_in.get(f"/issues/{iid}").text
    assert "Download failed" not in logged_in.get(f"/series/{oid}").text


def test_history_pages(app, logged_in):
    with app.state.session_factory() as db:
        for n in range(150):
            log(db, f"event {n:03}", kind="scan")
        db.commit()
    first = logged_in.get("/history").text
    assert "event 149" in first and "event 049" not in first and "page 1 of 2" in first
    assert "event 049" in logged_in.get("/history?page=2").text


def test_blocklist_remove_and_clear(app, logged_in):
    _data(app)
    page = logged_in.get("/blocklist").text
    assert "Saga 007 (bad)" in page and "download failed" in page
    with app.state.session_factory() as db:
        row_id = db.scalar(select(Blocklist.id))
    assert logged_in.post(f"/blocklist/{row_id}/remove", follow_redirects=False).status_code == 303
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count(Blocklist.id))) == 0
        db.add_all([Blocklist(title="a"), Blocklist(title="b")])
        db.commit()
    logged_in.post("/blocklist/clear")
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count(Blocklist.id))) == 0
        assert "Cleared the blocklist (2 releases)" in db.scalar(select(Event.message).order_by(Event.id.desc()))
    assert logged_in.post("/blocklist/999/remove").status_code == 404
