"""Story arcs from Metron as reading lists."""

import xml.etree.ElementTree as ET
from datetime import date

import respx
from sqlalchemy import func, select

from fumora import settings_store
from fumora.metron import BASE_URL
from fumora.models import File, Issue, Series, StoryArc, StoryArcEntry
from tests.test_m3_refresh import SERIES_DETAIL, page


def _entry(mid, sid, sname, number, year=2024, d="2024-07-01"):
    series = {"id": sid, "name": sname, "volume": 1, "year_began": year}
    return {"id": mid, "series": series, "number": number, "cover_date": d, "store_date": d}


ARC = {"id": 1606, "name": "Absolute Power", "desc": "Amanda Waller...", "cv_id": 1}
ENTRIES = [
    _entry(118371, 7525, "Absolute Power FCBD", "1", d="2024-05-01"),
    _entry(121602, 4781, "Green Arrow", "13", year=2023),
    _entry(121700, 500, "Monthly", "2"),
    _entry(121701, 500, "Monthly", "3"),
]


def _login(app):
    with app.state.session_factory() as db:
        settings_store.put(db, "metron.username", "u")
        settings_store.put(db, "metron.password", "p")
        db.commit()


def _followed(app, tmp_path):
    """Monthly is followed: #2 has a file, #3 is not monitored yet."""
    with app.state.session_factory() as db:
        s = Series(name="Monthly", year_began=2024, metron_id=500, cv_id=9, monitored=False, locked_fields=[])
        have = Issue(series=s, number="2", metron_id=121700, cv_id=902, cover_date=date(2024, 7, 1),
                     locked_fields=[])  # fmt: skip
        have.files.append(File(path=str(tmp_path / "m2.cbz"), size=1))
        db.add_all([s, have, Issue(series=s, number="3", metron_id=121701, cover_date=date(2024, 7, 1),
                                   locked_fields=[])])  # fmt: skip
        db.commit()
        return s.id


def _mock_arc(entries=ENTRIES):
    respx.get(f"{BASE_URL}arc/1606/").respond(200, json=ARC)
    respx.get(f"{BASE_URL}arc/1606/issue_list/").respond(200, json=page(entries))


@respx.mock
def test_follow_an_arc_and_see_what_you_have(app, logged_in, tmp_path):
    _login(app)
    _followed(app, tmp_path)
    respx.get(f"{BASE_URL}arc/", params={"name": "Absolute Power"}).respond(
        200, json=page([{"id": 1606, "name": "Absolute Power", "modified": "x"}])
    )
    _mock_arc()
    assert "Follow" in logged_in.get("/arcs?q=Absolute Power").text
    r = logged_in.post("/arcs/add/1606", follow_redirects=False)
    arc_url = r.headers["location"]
    text = logged_in.get(arc_url).text
    assert "4 issues: 1 in the library, 0 wanted, 2 in series you don't follow" in text
    assert "Green Arrow (2023) #13" in text and "Monthly #2" in text
    assert "Following" in logged_in.get("/arcs?q=Absolute Power").text


@respx.mock
def test_monitor_and_add_missing_series(app, logged_in, tmp_path):
    _login(app)
    sid = _followed(app, tmp_path)
    _mock_arc()
    arc_url = logged_in.post("/arcs/add/1606", follow_redirects=False).headers["location"]
    logged_in.post(f"{arc_url}/monitor")
    with app.state.session_factory() as db:
        three = db.scalar(select(Issue).where(Issue.metron_id == 121701))
        assert three.monitored and db.get(Series, sid).monitored
    # Add Green Arrow for the arc: only its arc issue gets monitored.
    ga = {**SERIES_DETAIL, "id": 4781, "name": "Green Arrow", "year_began": 2023, "cv_id": 77}
    respx.get(f"{BASE_URL}series/4781/").respond(200, json=ga)
    respx.get(f"{BASE_URL}issue/", params={"series_id": "4781"}).respond(200, json=page([
        {"id": 121601, "number": "12", "cover_date": "2024-07-01"},
        {"id": 121602, "number": "13", "cover_date": "2024-08-01"},
    ]))  # fmt: skip
    logged_in.post(f"{arc_url}/add-series", data={"series_ids": ["4781", "999999"]})
    with app.state.session_factory() as db:
        ga = db.scalar(select(Series).where(Series.metron_id == 4781))
        assert {i.number: i.monitored for i in ga.issues} == {"12": False, "13": True}
        assert db.scalar(select(func.count(Series.id)).where(Series.metron_id == 999999)) == 0
    assert "2 in series you don't follow" not in logged_in.get(arc_url).text


@respx.mock
def test_refresh_follows_metron_and_delete_keeps_series(app, logged_in, tmp_path):
    _login(app)
    _followed(app, tmp_path)
    _mock_arc()
    arc_url = logged_in.post("/arcs/add/1606", follow_redirects=False).headers["location"]
    respx.get(f"{BASE_URL}arc/1606/issue_list/").respond(
        200, json=page([ENTRIES[2], _entry(130000, 500, "Monthly", "9")])
    )
    logged_in.post(f"{arc_url}/refresh")
    with app.state.session_factory() as db:
        ids = [e.metron_issue_id for e in db.scalars(select(StoryArcEntry).order_by(StoryArcEntry.position))]
        assert ids == [121700, 130000]
    logged_in.post(f"{arc_url}/delete")
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count(StoryArc.id))) == 0
        assert db.scalar(select(func.count(StoryArcEntry.id))) == 0
        assert db.scalar(select(func.count(Series.id))) == 1


@respx.mock
def test_reading_list_export(app, logged_in, tmp_path):
    _login(app)
    _followed(app, tmp_path)
    _mock_arc()
    arc_url = logged_in.post("/arcs/add/1606", follow_redirects=False).headers["location"]
    r = logged_in.get(f"{arc_url}/reading-list.cbl")
    assert r.status_code == 200 and 'filename="Absolute Power.cbl"' in r.headers["content-disposition"]
    root = ET.fromstring(r.content)
    assert root.findtext("Name") == "Absolute Power"
    books = root.find("Books").findall("Book")
    assert [(b.get("Series"), b.get("Number")) for b in books] == [
        ("Absolute Power FCBD", "1"), ("Green Arrow", "13"), ("Monthly", "2"), ("Monthly", "3"),
    ]  # fmt: skip
    monthly2 = books[2]
    dbs = {d.get("Name"): (d.get("Series"), d.get("Issue")) for d in monthly2.findall("Database")}
    assert dbs == {"metron": ("500", "121700"), "cv": ("9", "902")}
    assert books[1].get("Year") == "2024" and books[1].get("Volume") == "2023"


@respx.mock
def test_issue_page_lists_its_arcs(app, logged_in, tmp_path):
    _login(app)
    _followed(app, tmp_path)
    _mock_arc()
    logged_in.post("/arcs/add/1606")
    with app.state.session_factory() as db:
        iid = db.scalar(select(Issue.id).where(Issue.metron_id == 121700))
    assert "Absolute Power" in logged_in.get(f"/issues/{iid}").text
