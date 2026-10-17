import sqlite3
from datetime import date

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

from fumora import monitoring
from fumora.db import MIGRATIONS_DIR
from fumora.models import File, Issue, Series

TODAY = date(2026, 10, 17)


def _series(db, owned=("2", "4"), numbers=("1", "2", "3", "4", "5", "6")):
    s = Series(name="S", monitored=True, locked_fields=[])
    for n in numbers:
        released = n != "6"
        i = Issue(series=s, number=n, cover_date=date(2025, int(n), 1) if released else date(2027, 1, 1),
                  locked_fields=[])  # fmt: skip
        if n in owned:
            i.files = [File(path=f"/x/{n}.cbz")]
        db.add(i)
    db.add(s)
    db.flush()
    return s


def _on(s):
    return sorted(i.number for i in s.issues if i.monitored)


@pytest.mark.parametrize(
    "option,expected",
    [
        ("all", ["1", "2", "3", "4", "5", "6"]),
        ("future", ["6"]),
        ("missing", ["1", "3", "5", "6"]),
        ("existing", ["2", "4", "6"]),
        ("gaps", ["2", "3", "4", "6"]),  # 3 lies between owned 2 and 4; 1 and 5 don't
        ("none", []),
    ],
)
def test_monitor_options(app, option, expected):
    with app.state.session_factory() as db:
        s = _series(db)
        monitoring.apply_option(s, option, TODAY)
        assert _on(s) == expected
        assert s.monitored == (option != "none")


def test_states_and_wanted_count(app):
    with app.state.session_factory() as db:
        s = _series(db)
        monitoring.apply_option(s, "missing", TODAY)
        states = {i.number: monitoring.state(i, TODAY) for i in s.issues}
        assert states == {"1": "Wanted", "2": "Downloaded", "3": "Wanted", "4": "Downloaded",
                          "5": "Wanted", "6": "Unreleased"}  # fmt: skip
        assert monitoring.counts(db, s.id, TODAY) == {"wanted": 3, "missing": 0}
        s.monitored = False  # an unmonitored series wants nothing
        db.flush()
        assert monitoring.counts(db, s.id, TODAY) == {"wanted": 0, "missing": 3}


def test_new_issue_rule():
    s = Series(name="S", monitored=True, monitor_new=True)
    added = date(2026, 10, 1)
    assert monitoring.new_issue_monitored(s, date(2026, 11, 5), added)
    assert monitoring.new_issue_monitored(s, None, added)
    assert not monitoring.new_issue_monitored(s, date(2024, 1, 1), added)  # an old gap
    s.monitor_new = False
    assert not monitoring.new_issue_monitored(s, date(2026, 11, 5), added)


def test_migration_0004_derives_monitoring_from_mylar_status(tmp_path):
    db = tmp_path / "m.db"
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(cfg, "0003")
    c = sqlite3.connect(db)
    c.execute("INSERT INTO series (id, name, monitored, locked_fields, added, match_status, source) "
              "VALUES (1, 'S', 1, '[]', '2026-10-16 10:00:00', 'matched', 'metron')")  # fmt: skip
    c.execute("INSERT INTO series (id, name, monitored, locked_fields, added, match_status, source) "
              "VALUES (2, 'Paused', 0, '[]', '2026-10-16 10:00:00', 'matched', 'metron')")  # fmt: skip
    c.execute("INSERT INTO issue (id, series_id, number, status, monitored, locked_fields) "
              "VALUES (9, 2, 'p1', 'Downloaded', 1, '[]')")  # fmt: skip
    rows = [
        (1, "1", "Downloaded", None, None),
        (2, "2", "Wanted", None, None),
        (3, "3", "Skipped", None, None),
        (4, "4", "Skipped", "new", "2024-05-01"),  # old gap Metron added
        (5, "5", "Skipped", "new", "2026-12-01"),  # future issue Metron added
    ]
    for iid, n, status, ms, d in rows:
        c.execute(
            "INSERT INTO issue (id, series_id, number, status, monitored, locked_fields, match_status, "
            "cover_date) VALUES (?, 1, ?, ?, 1, '[]', ?, ?)",
            (iid, n, status, ms, d),
        )
    c.commit()
    command.upgrade(cfg, "head")
    got = dict(c.execute("SELECT number, monitored FROM issue").fetchall())
    assert got == {"1": 1, "2": 1, "3": 0, "4": 0, "5": 1, "p1": 0}  # p1: series not active
    assert c.execute("SELECT monitor_new FROM series").fetchone()[0] == 1
