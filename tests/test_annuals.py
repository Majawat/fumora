"""Annuals and specials belong to their main series (Mylar's annuals integration)."""

import respx
from sqlalchemy import select

from fumora import annuals, settings_store
from fumora.importer import upsert_mylar
from fumora.metron import BASE_URL
from fumora.models import File, Issue, Publisher, Series
from fumora.mylar import MylarIssue, MylarSeries
from tests.test_m3_refresh import SERIES_DETAIL, page


def _s(db, name, year=None, pub=None, end=None, **kw):
    s = Series(name=name, year_began=year, year_end=end, publisher=pub, locked_fields=[], **kw)
    db.add(s)
    db.flush()
    return s


def test_guess_parent_picks_the_run_going_when_the_annual_began(app):
    with app.state.session_factory() as db:
        dc, marvel = Publisher(name="DC"), Publisher(name="Marvel")
        old = _s(db, "Batman", 2011, dc, end=2016)
        new = _s(db, "Batman", 2016, dc)
        _s(db, "Batman Beyond", 2016, dc)
        annual = _s(db, "Batman Annual", 2017, dc, series_type="Annual")
        assert annuals.guess_parent(db, annual).id == new.id
        early = _s(db, "Batman Annual", 2012, dc, series_type="Annual")
        assert annuals.guess_parent(db, early).id == old.id
        other_pub = _s(db, "Batman Annual", 2017, marvel, series_type="Annual")
        assert annuals.guess_parent(db, other_pub) is None
        assert annuals.guess_parent(db, new) is None  # not an annual
        special = _s(db, "Batman Special", 2017, dc)
        assert annuals.guess_parent(db, special).id == new.id
        names = [p.name for p in annuals.parent_candidates(db, special)]
        assert names[:2] == ["Batman", "Batman"] and "Batman Beyond" not in names


def test_link_moves_a_new_annual_into_the_parent_folder_only_without_files(app, tmp_path):
    with app.state.session_factory() as db:
        parent = _s(db, "Saga", 2012, path=str(tmp_path / "Saga (2012)"))
        empty = _s(db, "Saga Annual", 2013, path=str(tmp_path / "Saga Annual (2013)"))
        annuals.link(empty, parent)
        assert empty.parent_id == parent.id and empty.path == parent.path
        full = _s(db, "Saga Special", 2014, path=str(tmp_path / "Saga Special (2014)"))
        i = Issue(series=full, number="1", locked_fields=[])
        i.files.append(File(path=str(tmp_path / "x.cbz"), size=1))
        db.add(i)
        db.flush()
        annuals.link(full, parent)
        assert full.path == str(tmp_path / "Saga Special (2014)")  # has files: stays put
        try:
            annuals.link(parent, empty)
        except ValueError:
            pass
        else:
            raise AssertionError("a series can't belong to its own annual")


def _mi(cv):
    return MylarIssue(cv_id=cv, number="1", title=None, cover_date=None, store_date=None, status="Wanted",
                      path=None)  # fmt: skip


def test_mylar_import_keeps_mylars_annual_link(app):
    parent = MylarSeries(cv_id=1, name="Saga", sort_name=None, year=2012, publisher="Image", path="/x/Saga",
                         active=True, mylar_type=None, age_rating=None,
                         issues=[_mi(11)])  # fmt: skip
    annual = MylarSeries(cv_id=2, name="Saga Annual", sort_name=None, year=2014, publisher="Image",
                         path="/x/Saga", active=True, mylar_type="Annual", age_rating=None, is_annual=True,
                         parent_cv_id=1,
                         issues=[_mi(21)])  # fmt: skip
    with app.state.session_factory() as db:
        upsert_mylar(db, [parent, annual], exists=lambda p: False)
        db.commit()
        a = db.scalar(select(Series).where(Series.cv_id == 2))
        assert a.parent is not None and a.parent.cv_id == 1


ANNUAL_DETAIL = {**SERIES_DETAIL, "id": 600, "name": "Monthly Annual", "year_began": 2025, "cv_id": 10,
                 "series_type": {"id": 6, "name": "Annual"}, "issue_count": 1}  # fmt: skip


@respx.mock
def test_add_page_offers_the_annuals_and_adds_them_into_the_folder(app, logged_in, tmp_path):
    with app.state.session_factory() as db:
        settings_store.put(db, "metron.username", "u")
        settings_store.put(db, "metron.password", "p")
        settings_store.put(db, "library.path", str(tmp_path))
        db.commit()
    respx.get(f"{BASE_URL}series/500/").respond(200, json=SERIES_DETAIL)
    respx.get(f"{BASE_URL}series/600/").respond(200, json=ANNUAL_DETAIL)
    respx.get(f"{BASE_URL}series/", params={"name": "Monthly Annual"}).respond(200, json=page([
        {"id": 600, "series": "Monthly Annual (2025)", "year_began": 2025, "issue_count": 1},
        {"id": 700, "series": "Monthly Annual (1980)", "year_began": 1980},  # another era
        {"id": 800, "series": "Monthly Annualized (2025)", "year_began": 2025},  # another name
    ]))  # fmt: skip
    respx.get(f"{BASE_URL}issue/", params={"series_id": "500"}).respond(
        200, json=page([{"id": 5001, "number": "1", "cover_date": "2025-01-01"}])
    )
    respx.get(f"{BASE_URL}issue/", params={"series_id": "600"}).respond(
        200, json=page([{"id": 6001, "number": "1", "cover_date": "2025-06-01"}])
    )
    form = logged_in.get("/add/500").text
    assert 'value="600"' in form and 'value="700"' not in form and 'value="800"' not in form
    r = logged_in.post("/add/500", data={"option": "all", "annual_ids": ["600"]}, follow_redirects=False)
    assert r.status_code == 303
    with app.state.session_factory() as db:
        main = db.scalar(select(Series).where(Series.metron_id == 500))
        annual = db.scalar(select(Series).where(Series.metron_id == 600))
        assert annual.parent_id == main.id and annual.path == main.path
        assert all(i.monitored for i in annual.issues)
    page_html = logged_in.get(f"/series/{main.id}").text
    assert "Annuals and specials" in page_html and "Monthly Annual" in page_html
    assert "Annual/special of" in logged_in.get(f"/series/{annual.id}").text


def test_set_and_clear_the_parent_by_hand(app, logged_in):
    with app.state.session_factory() as db:
        p = _s(db, "Saga", 2012)
        a = _s(db, "Saga Annual", 2013)
        db.commit()
        pid, aid = p.id, a.id
    assert f'value="{pid}"' in logged_in.get(f"/series/{aid}").text
    logged_in.post(f"/series/{aid}/parent", data={"parent_id": str(pid)})
    with app.state.session_factory() as db:
        assert db.get(Series, aid).parent_id == pid
    assert logged_in.post(f"/series/{pid}/parent", data={"parent_id": str(aid)}).status_code == 400
    logged_in.post(f"/series/{aid}/parent", data={"parent_id": ""})
    with app.state.session_factory() as db:
        assert db.get(Series, aid).parent_id is None


def test_year_in_the_name_and_linking_once_after_the_upgrade(app, config):
    from fumora.main import create_app

    with app.state.session_factory() as db:
        flash = _s(db, "The Flash", 2016)
        a = _s(db, "The Flash 2021 Annual", 2021)
        b = _s(db, "My Little Pony Annual 2021", 2021)
        pony = _s(db, "My Little Pony", 2012)
        db.commit()
        ids = (a.id, b.id, flash.id, pony.id)
    from fumora import settings_store

    with app.state.session_factory() as db:  # the first start already linked (nothing then)
        settings_store.put_state(db, "annuals.linked_once", None)
        db.commit()
    again = create_app(config)
    with again.state.session_factory() as db:
        assert db.get(Series, ids[0]).parent_id == ids[2]
        assert db.get(Series, ids[1]).parent_id == ids[3]
        db.get(Series, ids[0]).parent_id = None  # unlinked by hand: stays unlinked
        db.commit()
    third = create_app(config)
    with third.state.session_factory() as db:
        assert db.get(Series, ids[0]).parent_id is None
