import httpx
import respx

from fumora import settings_store
from fumora.metron import BASE_URL
from fumora.models import File, Issue, Series


def add_series(app):
    with app.state.session_factory() as db:
        s = Series(
            name="Private Dance", year_began=2023, cv_id=162288, match_status="not_found", locked_fields=[]
        )
        db.add(s)
        db.flush()
        i1 = Issue(series_id=s.id, number="1", sort_number=1, status="Downloaded", cv_id=1, locked_fields=[])
        db.add(i1)
        db.flush()
        db.add(File(issue_id=i1.id, path="/data/media/Comics/PD/Private Dance 001 (2023).cbz"))
        settings_store.put(db, "metron.username", "u")
        settings_store.put(db, "metron.password", "p")
        db.commit()
        return s.id


def test_series_pages_render(logged_in, app):
    sid = add_series(app)
    r = logged_in.get("/series")
    assert "Private Dance (2023)" in r.text and "1/1" in r.text
    assert "Private Dance" in logged_in.get("/series?match=not_found").text
    assert "Private Dance" not in logged_in.get("/series?match=matched").text.split("<tbody>")[1]
    r = logged_in.get(f"/series/{sid}")
    assert r.status_code == 200 and "Private Dance 001 (2023).cbz" in r.text
    assert logged_in.get("/series/9999").status_code == 404


def test_import_page_renders(logged_in, app):
    add_series(app)
    r = logged_in.get("/import")
    assert r.status_code == 200
    assert "Needs attention (1)" in r.text
    assert "Start import" in r.text


@respx.mock
def test_manual_match(logged_in, app):
    sid = add_series(app)
    respx.get(f"{BASE_URL}series/17753/").respond(
        200, json={"id": 17753, "name": "Private Dance", "year_began": 2023}
    )
    respx.get(f"{BASE_URL}issue/").respond(
        200,
        json={
            "count": 2,
            "next": None,
            "previous": None,
            "results": [
                {"id": 186450, "number": "1", "cover_date": "2023-09-01"},
                {"id": 186451, "number": "2", "cover_date": "2024-05-01"},
            ],
        },
    )
    r = logged_in.post(f"/series/{sid}/metron", data={"metron_id": "17753"}, follow_redirects=False)
    assert r.status_code == 303
    with app.state.session_factory() as db:
        s = db.get(Series, sid)
        assert s.metron_id == 17753 and s.match_status == "matched"
        assert sorted(i.number for i in s.issues) == ["1", "2"]

    # Matching another series to the same Metron id is refused.
    with app.state.session_factory() as db:
        other = Series(name="Other", locked_fields=[])
        db.add(other)
        db.commit()
        oid = other.id
    r = logged_in.post(f"/series/{oid}/metron", data={"metron_id": "17753"})
    assert r.status_code == 400 and "already matched" in r.text


def test_httpx_import_used():
    assert httpx
