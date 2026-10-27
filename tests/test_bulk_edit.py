from datetime import date

import pytest
from sqlalchemy import select

from fumora.models import Event, File, Issue, Series, Tag


def _make_series(db, name: str, **kwargs) -> Series:
    defaults = {
        "year_began": 2020,
        "match_status": "matched",
        "monitored": False,
        "monitor_new": True,
        "numbering": "auto",
        "reading_direction": None,
        "age_rating_override": None,
        "locked_fields": [],
    }
    defaults.update(kwargs)
    s = Series(name=name, **defaults)
    db.add(s)
    db.flush()
    return s


def test_series_page_renders_bulk_edit_form_and_checkboxes(logged_in, app):
    with app.state.session_factory() as db:
        s1 = _make_series(db, "Saga", year_began=2012)
        s2 = _make_series(db, "Paper Girls", year_began=2015)
        db.commit()
        id1, id2 = s1.id, s2.id

    r = logged_in.get("/series")
    assert r.status_code == 200
    assert 'action="/series/bulk-edit"' in r.text
    assert 'aria-label="Select all shown"' in r.text
    assert f'name="series_ids" value="{id1}"' in r.text
    assert f'name="series_ids" value="{id2}"' in r.text
    for field in (
        "monitored",
        "monitor_new",
        "monitor_option",
        "numbering",
        "reading_direction",
        "age_rating",
        "add_tags",
        "remove_tags",
    ):
        assert f'name="{field}"' in r.text


def test_bulk_edit_applies_each_field_and_leaves_unselected_untouched(logged_in, app):
    with app.state.session_factory() as db:
        s1 = _make_series(
            db,
            "Saga",
            monitored=False,
            monitor_new=True,
            numbering="auto",
            reading_direction="ltr",
            age_rating_override="Teen",
        )
        s2 = _make_series(
            db,
            "Berserk",
            monitored=False,
            monitor_new=False,
            numbering="issues",
            reading_direction=None,
            age_rating_override=None,
        )
        s3 = _make_series(
            db,
            "Untouched",
            monitored=False,
            monitor_new=True,
            numbering="auto",
            reading_direction="ltr",
            age_rating_override="Everyone",
        )
        db.commit()
        id1, id2, id3 = s1.id, s2.id, s3.id

    # Change monitored, monitor_new, numbering, reading_direction, age_rating on s1 and s2.
    r = logged_in.post(
        "/series/bulk-edit",
        data={
            "series_ids": [str(id1), str(id2)],
            "monitored": "on",
            "monitor_new": "off",
            "numbering": "volumes",
            "reading_direction": "rtl",
            "age_rating": "Mature",
        },
    )
    assert r.status_code == 200
    assert "Changed 2 series" in r.text

    with app.state.session_factory() as db:
        for sid in (id1, id2):
            s = db.get(Series, sid)
            assert s.monitored is True
            assert s.monitor_new is False
            assert s.numbering == "volumes"
            assert s.reading_direction == "rtl"
            assert s.age_rating_override == "Mature"

        u = db.get(Series, id3)
        assert u.monitored is False
        assert u.monitor_new is True
        assert u.numbering == "auto"
        assert u.reading_direction == "ltr"
        assert u.age_rating_override == "Everyone"

    # Now test clearing reading_direction to unknown (None) and age_rating to calculated (None),
    # while leaving monitored, monitor_new, and numbering at "No change".
    r2 = logged_in.post(
        "/series/bulk-edit",
        data={
            "series_ids": [str(id1)],
            "reading_direction": "unknown",
            "age_rating": "calculated",
        },
    )
    assert r2.status_code == 200
    with app.state.session_factory() as db:
        s1_after = db.get(Series, id1)
        assert s1_after.reading_direction is None
        assert s1_after.age_rating_override is None
        # "No change" fields stayed as they were:
        assert s1_after.monitored is True
        assert s1_after.monitor_new is False
        assert s1_after.numbering == "volumes"

        # s2 was not selected in r2:
        s2_after = db.get(Series, id2)
        assert s2_after.reading_direction == "rtl"
        assert s2_after.age_rating_override == "Mature"


def test_bulk_edit_monitoring_option_updates_issues(logged_in, app):
    with app.state.session_factory() as db:
        s1 = _make_series(db, "Run A", monitored=False)
        s2 = _make_series(db, "Run B", monitored=False)
        for s in (s1, s2):
            for n in ("1", "2", "3", "4", "5"):
                future = n == "5"
                issue = Issue(
                    series=s,
                    number=n,
                    sort_number=float(n),
                    cover_date=date(2099, 1, 1) if future else date(2024, int(n), 1),
                    monitored=False,
                    locked_fields=[],
                )
                if n in ("2", "4"):
                    issue.files = [File(path=f"/lib/{s.name}/{n}.cbz")]
                db.add(issue)
        db.commit()
        id1, id2 = s1.id, s2.id

    r = logged_in.post(
        "/series/bulk-edit",
        data={"series_ids": [str(id1)], "monitor_option": "gaps"},
    )
    assert r.status_code == 200
    assert "monitoring set to Fill gaps + future" in r.text

    with app.state.session_factory() as db:
        s1_db = db.get(Series, id1)
        assert s1_db.monitored is True
        monitored_nums = sorted(i.number for i in s1_db.issues if i.monitored)
        assert monitored_nums == ["2", "3", "4", "5"]

        s2_db = db.get(Series, id2)
        assert s2_db.monitored is False
        assert [i.number for i in s2_db.issues if i.monitored] == []


def test_bulk_edit_tags_case_insensitive_without_duplicates(logged_in, app):
    with app.state.session_factory() as db:
        t_horror = Tag(name="Horror")
        t_scifi = Tag(name="Sci-Fi")
        t_romance = Tag(name="Romance")
        db.add_all([t_horror, t_scifi, t_romance])
        db.flush()

        s1 = _make_series(db, "Series One")
        s1.tags = [t_horror, t_scifi]
        s2 = _make_series(db, "Series Two")
        s2.tags = [t_romance]
        s3 = _make_series(db, "Series Three")
        s3.tags = [t_scifi]
        db.commit()
        id1, id2, id3 = s1.id, s2.id, s3.id

    r = logged_in.post(
        "/series/bulk-edit",
        data={
            "series_ids": [str(id1), str(id2)],
            "add_tags": "horror, Fantasy, FANTASY",
            "remove_tags": "sci-fi, NonExistent",
        },
    )
    assert r.status_code == 200
    assert "tags added: horror, Fantasy" in r.text
    assert "tags removed: sci-fi, NonExistent" in r.text

    with app.state.session_factory() as db:
        assert sorted(t.name for t in db.get(Series, id1).tags) == ["Fantasy", "Horror"]
        assert sorted(t.name for t in db.get(Series, id2).tags) == ["Fantasy", "Horror", "Romance"]
        # Unselected series keeps its Sci-Fi tag:
        assert [t.name for t in db.get(Series, id3).tags] == ["Sci-Fi"]
        # No duplicate Tag rows, and removing a non-existent tag did not create a Tag row:
        all_tags = sorted(t.name for t in db.scalars(select(Tag)).all())
        assert all_tags == ["Fantasy", "Horror", "Romance", "Sci-Fi"]


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("monitored", "maybe"),
        ("monitored", "true"),
        ("monitored", "false"),
        ("monitored", "monitored"),
        ("monitored", "not_monitored"),
        ("monitored", "unmonitored"),
        ("monitor_new", "yesterday"),
        ("monitor_new", "true"),
        ("monitor_new", "false"),
        ("monitor_option", "every_second_issue"),
        ("numbering", "chapters"),
        ("reading_direction", "diagonal"),
        ("reading_direction", "none"),
        ("age_rating", "R-18"),
        ("age_rating", "none"),
    ],
)
def test_bulk_edit_invalid_value_returns_400_and_changes_nothing(logged_in, app, field, bad_value):
    with app.state.session_factory() as db:
        s = _make_series(db, "Saga", monitored=False, numbering="auto")
        db.commit()
        sid = s.id

    payload = {
        "series_ids": [str(sid)],
        "monitored": "on",
        "numbering": "volumes",
        field: bad_value,
    }
    r = logged_in.post("/series/bulk-edit", data=payload)
    assert r.status_code == 400
    assert field in r.text and bad_value in r.text

    with app.state.session_factory() as db:
        s_after = db.get(Series, sid)
        assert s_after.monitored is False
        assert s_after.numbering == "auto"
        assert db.scalars(select(Event).where(Event.series_id == sid)).all() == []


def test_bulk_edit_same_tag_added_and_removed_returns_400_and_changes_nothing(logged_in, app):
    with app.state.session_factory() as db:
        t_horror = Tag(name="Horror")
        db.add(t_horror)
        db.flush()
        s = _make_series(db, "Saga", monitored=False)
        s.tags = [t_horror]
        db.commit()
        sid = s.id

    r = logged_in.post(
        "/series/bulk-edit",
        data={
            "series_ids": [str(sid)],
            "monitored": "on",
            "add_tags": "Fantasy, Sci-Fi",
            "remove_tags": "sci-fi, Horror",
        },
    )
    assert r.status_code == 400
    assert "sci-fi" in r.text.lower()

    with app.state.session_factory() as db:
        s_after = db.get(Series, sid)
        assert s_after.monitored is False
        assert [t.name for t in s_after.tags] == ["Horror"]
        assert [t.name for t in db.scalars(select(Tag)).all()] == ["Horror"]
        assert db.scalars(select(Event).where(Event.series_id == sid)).all() == []


def test_bulk_edit_no_selection_and_all_no_change_notices(logged_in, app):
    with app.state.session_factory() as db:
        s = _make_series(db, "Saga", monitored=False)
        db.commit()
        sid = s.id

    r_none = logged_in.post("/series/bulk-edit", data={"monitored": "on"})
    assert r_none.status_code == 200
    assert "No series selected; nothing changed." in r_none.text

    r_no_change = logged_in.post(
        "/series/bulk-edit",
        data={"series_ids": [str(sid)], "add_tags": " , "},
    )
    assert r_no_change.status_code == 200
    assert "No changes selected; nothing changed." in r_no_change.text

    with app.state.session_factory() as db:
        assert db.get(Series, sid).monitored is False
        assert db.scalars(select(Event).where(Event.series_id == sid)).all() == []


def test_bulk_edit_unknown_ids_reported_while_rest_apply(logged_in, app):
    with app.state.session_factory() as db:
        s = _make_series(db, "Saga", monitored=False)
        db.commit()
        sid = s.id

    r = logged_in.post(
        "/series/bulk-edit",
        data={
            "series_ids": [str(sid), "88888", "99999"],
            "monitored": "on",
        },
    )
    assert r.status_code == 200
    assert "Changed 1 series: monitored on." in r.text
    assert "88888" in r.text and "99999" in r.text

    with app.state.session_factory() as db:
        assert db.get(Series, sid).monitored is True


def test_bulk_edit_logs_one_history_event_per_changed_series(logged_in, app):
    with app.state.session_factory() as db:
        t_horror = Tag(name="Horror")
        db.add(t_horror)
        db.flush()
        # s1: both monitored and reading_direction will change, plus Fantasy added
        s1 = _make_series(db, "Saga", monitored=False, reading_direction=None)
        s1.tags = [t_horror]
        # s2: already monitored=True, so only reading_direction changes (already has Horror too)
        s2 = _make_series(db, "Monstress", monitored=True, reading_direction="ltr")
        s2.tags = [t_horror]
        # s3: already monitored=True, reading_direction="rtl", has Horror, no Ghost -> nothing changes
        s3 = _make_series(db, "Paper Girls", monitored=True, reading_direction="rtl")
        s3.tags = [t_horror]
        # s4: not selected at all
        s4 = _make_series(db, "Other", monitored=False, reading_direction=None)
        db.commit()
        id1, id2, id3, id4 = s1.id, s2.id, s3.id, s4.id

    r = logged_in.post(
        "/series/bulk-edit",
        data={
            "series_ids": [str(id1), str(id2), str(id3)],
            "monitored": "on",
            "reading_direction": "rtl",
            "add_tags": "Horror",
            "remove_tags": "Ghost",
        },
    )
    assert r.status_code == 200
    assert (
        "Changed 2 series: monitored on, reading direction rtl, tags added: Horror, tags removed: Ghost."
        " 1 already had these settings."
    ) in r.text

    with app.state.session_factory() as db:
        ev1 = db.scalars(select(Event).where(Event.series_id == id1)).all()
        ev2 = db.scalars(select(Event).where(Event.series_id == id2)).all()
        ev3 = db.scalars(select(Event).where(Event.series_id == id3)).all()
        ev4 = db.scalars(select(Event).where(Event.series_id == id4)).all()
        assert len(ev1) == 1
        assert ev1[0].kind == "series"
        assert ev1[0].message == "'Saga': monitored on, reading direction rtl"
        assert len(ev2) == 1
        assert ev2[0].kind == "series"
        assert ev2[0].message == "'Monstress': reading direction rtl"
        assert ev3 == []
        assert ev4 == []


def test_bulk_edit_issue_monitored_change_counts_as_real_change(logged_in, app):
    with app.state.session_factory() as db:
        # s1 is already monitored=True, but issue #1 is unmonitored -> monitor_option="all" changes issue #1
        s1 = _make_series(db, "Saga", monitored=True)
        db.add(
            Issue(
                series=s1,
                number="1",
                sort_number=1.0,
                cover_date=date(2024, 1, 1),
                monitored=False,
                locked_fields=[],
            )
        )
        # s2 is already monitored=True and issue #1 is already monitored=True -> nothing changes
        s2 = _make_series(db, "Monstress", monitored=True)
        db.add(
            Issue(
                series=s2,
                number="1",
                sort_number=1.0,
                cover_date=date(2024, 1, 1),
                monitored=True,
                locked_fields=[],
            )
        )
        db.commit()
        id1, id2 = s1.id, s2.id

    r = logged_in.post(
        "/series/bulk-edit",
        data={"series_ids": [str(id1), str(id2)], "monitor_option": "all"},
    )
    assert r.status_code == 200
    assert "Changed 1 series: monitoring set to All issues. 1 already had these settings." in r.text

    with app.state.session_factory() as db:
        ev1 = db.scalars(select(Event).where(Event.series_id == id1)).all()
        ev2 = db.scalars(select(Event).where(Event.series_id == id2)).all()
        assert len(ev1) == 1
        assert ev1[0].kind == "monitor"
        assert ev1[0].message == "'Saga': monitoring set to All issues"
        assert ev2 == []


def test_bulk_edit_keeps_filters_on_way_back(logged_in, app):
    with app.state.session_factory() as db:
        s1 = _make_series(db, "Saga", match_status="matched")
        _make_series(db, "Sandman", match_status="matched")
        _make_series(db, "Saga of the Swamp Thing", match_status="not_found")
        _make_series(db, "Batman", match_status="matched")
        db.commit()
        sid = s1.id

    r = logged_in.post(
        "/series/bulk-edit",
        data={
            "series_ids": [str(sid)],
            "monitored": "on",
            "match": "matched",
            "q": "Sa",
        },
    )
    assert r.status_code == 200
    assert "Changed 1 series: monitored on." in r.text
    tbody = r.text.split("<tbody>")[1].split("</tbody>")[0]
    assert "Saga" in tbody
    assert "Sandman" in tbody
    assert "Swamp Thing" not in tbody
    assert "Batman" not in tbody
    assert 'name="q" value="Sa"' in r.text
    assert '<option value="matched" selected>' in r.text
