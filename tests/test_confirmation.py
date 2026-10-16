from sqlalchemy import select

from fumora.importer import decide_confirmation, review_confirmations, unmatch_series
from fumora.models import File, Issue, Series


def _matched(**kw):
    base = dict(match_status="matched", metron_id=1, locked_fields=[], source="metron")
    return Series(**{**base, **kw})


def test_auto_confirm_when_names_agree():
    s = _matched(
        name="The Umbrella Academy: Dallas",
        year_began=2008,
        mylar_name="Umbrella Academy Dallas",
        mylar_year=2009,
    )
    decide_confirmation(s)
    assert s.confirmed_by == "auto"


def test_different_name_needs_the_user():
    s = _matched(name="Harbinger: Omegas", year_began=2014, mylar_name="Trillium", mylar_year=2014)
    decide_confirmation(s)
    assert s.confirmed_by is None and "Trillium" in s.match_note


def test_year_far_apart_needs_the_user():
    s = _matched(name="Uncle Scrooge", year_began=1953, mylar_name="Uncle Scrooge", mylar_year=1963)
    decide_confirmation(s)
    assert s.confirmed_by is None


def test_review_backfills_existing_matches(app):
    with app.state.session_factory() as db:
        db.add_all([
            _matched(name="A", mylar_name="A", metron_id=1),
            _matched(name="B", mylar_name="Other", metron_id=2),
            _matched(name="C", mylar_name="Old", metron_id=3, match_note="Matched by hand"),
        ])  # fmt: skip
        db.flush()
        assert review_confirmations(db) == 1
        by = {s.name: s.confirmed_by for s in db.scalars(select(Series))}
        assert by == {"A": "auto", "B": None, "C": "user"}


def test_unmatch_restores_mylar_name_and_drops_metron_only_issues(app):
    with app.state.session_factory() as db:
        s = _matched(name="Harbinger: Omegas", year_began=2014, mylar_name="Trillium", mylar_year=2013,
                     confirmed_by="auto")  # fmt: skip
        kept = Issue(series=s, number="1", metron_id=11, match_status="matched", locked_fields=[])
        kept.files = [File(path="/x/1.cbz")]
        gone = Issue(series=s, number="9", metron_id=19, match_status="new", locked_fields=[])
        db.add_all([s, kept, gone])
        db.flush()
        unmatch_series(db, s, "not it")
        assert (s.name, s.year_began, s.metron_id, s.match_status) == ("Trillium", 2013, None, "not_found")
        assert [i.number for i in s.issues] == ["1"] and kept.metron_id is None
