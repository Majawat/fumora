"""Database backups: made safely, listed, pruned, downloaded, uploaded, restored at the next start."""

import asyncio
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from fumora import backup, settings_store
from fumora.main import create_app
from fumora.models import Event, Series
from fumora.perms import FILE_MODE
from fumora.scheduler import backup_due, scheduled_backup


def _add(app, name):
    with app.state.session_factory() as db:
        db.add(Series(name=name, locked_fields=[]))
        db.commit()


def _names(app):
    with app.state.session_factory() as db:
        return sorted(db.scalars(select(Series.name)))


def test_backup_is_a_consistent_copy_with_fumora_permissions(app, config):
    _add(app, "Saga")
    made = backup.make_backup(config.db_path, backup.backup_dir(config.config_dir), "manual")
    assert (os.stat(made).st_mode & 0o777) == FILE_MODE
    con = sqlite3.connect(made)
    assert con.execute("SELECT name FROM series").fetchall() == [("Saga",)]  # the WAL's rows too
    con.close()
    assert backup.check(made, backup.known_revisions())
    assert [b.label for b in backup.list_backups(made.parent)] == ["manual"]


def test_restore_is_applied_at_the_next_start(app, config):
    _add(app, "Saga")
    folder = backup.backup_dir(config.config_dir)
    saved = backup.make_backup(config.db_path, folder, "manual")
    _add(app, "Paper Girls")  # after the backup
    backup.stage_restore(config.config_dir, saved)
    assert _names(app) == ["Paper Girls", "Saga"]  # nothing changes while running
    again = create_app(config)
    assert _names(again) == ["Saga"]
    assert not backup.restore_pending(config.config_dir)
    pre = [b for b in backup.list_backups(folder) if b.label == "pre-restore"]
    assert len(pre) == 1  # the replaced database was kept
    con = sqlite3.connect(pre[0].path)
    assert sorted(r[0] for r in con.execute("SELECT name FROM series")) == ["Paper Girls", "Saga"]
    con.close()
    with again.state.session_factory() as db:
        assert "restored" in db.scalar(select(Event.message).where(Event.kind == "backup"))


def test_a_bad_staged_restore_is_set_aside_not_applied(app, config):
    _add(app, "Saga")
    (config.config_dir / backup.PENDING).write_bytes(b"not a database at all" * 100)
    again = create_app(config)
    assert _names(again) == ["Saga"]
    assert list(config.config_dir.glob("restore-rejected-*.db"))


@pytest.mark.parametrize("kind", ["garbage", "other_app", "future"])
def test_check_refuses(tmp_path, app, config, kind):
    p = tmp_path / "x.db"
    if kind == "garbage":
        p.write_bytes(b"\x00" * 4096)
    else:
        con = sqlite3.connect(p)
        if kind == "other_app":
            con.execute("CREATE TABLE movies (id)")
        else:
            for t in ("series", "issue", "setting"):
                con.execute(f"CREATE TABLE {t} (id)")
            con.execute("CREATE TABLE alembic_version (version_num)")
            con.execute("INSERT INTO alembic_version VALUES ('9999')")
        con.commit()
        con.close()
    with pytest.raises(backup.BackupError):
        backup.stage_restore(config.config_dir, p)
    assert not backup.restore_pending(config.config_dir)


def test_prune_keeps_the_newest_scheduled_and_every_manual(tmp_path):
    folder = tmp_path / "b"
    folder.mkdir()
    for d in range(6):
        (folder / f"fumora-2026010{d + 1}-000000-scheduled.db").write_bytes(b"x")
    (folder / "fumora-20250101-000000-manual.db").write_bytes(b"x")
    (folder / "notes.txt").write_text("not ours")
    gone = backup.prune(folder, keep=2)
    assert len(gone) == 4
    assert sorted(p.name for p in folder.iterdir()) == [
        "fumora-20250101-000000-manual.db",
        "fumora-20260105-000000-scheduled.db",
        "fumora-20260106-000000-scheduled.db",
        "notes.txt",
    ]


def test_backups_page_now_download_delete_and_paths(app, config, logged_in):
    r = logged_in.post("/backups/now")
    assert r.status_code == 200 and "Saved fumora-" in r.text
    name = backup.list_backups(backup.backup_dir(config.config_dir))[0].name
    assert name in logged_in.get("/backups").text
    d = logged_in.get(f"/backups/{name}/download")
    assert d.status_code == 200 and d.content.startswith(b"SQLite format 3")
    assert logged_in.get("/backups/..%2Ffumora.db/download").status_code == 404
    assert logged_in.get("/backups/fumora.db/download").status_code == 404
    logged_in.post(f"/backups/{name}/delete")
    assert backup.list_backups(backup.backup_dir(config.config_dir)) == []


def test_upload_then_restore_from_the_page(app, config, logged_in, tmp_path):
    _add(app, "Saga")
    src = backup.make_backup(config.db_path, tmp_path / "elsewhere", "manual")
    r = logged_in.post("/backups/upload", files={"upload": ("mine.db", src.read_bytes())})
    assert r.status_code == 200 and "Uploaded as" in r.text
    up = [b for b in backup.list_backups(backup.backup_dir(config.config_dir)) if b.label == "uploaded"]
    assert len(up) == 1
    bad = logged_in.post("/backups/upload", files={"upload": ("x.db", b"nope" * 300)})
    assert bad.status_code == 400 and len(backup.list_backups(backup.backup_dir(config.config_dir))) == 1
    r = logged_in.post(f"/backups/{up[0].name}/restore")
    assert r.status_code == 200 and backup.restore_pending(config.config_dir)
    assert "A restore is waiting" in logged_in.get("/backups").text
    logged_in.post("/backups/cancel-restore")
    assert not backup.restore_pending(config.config_dir)


def test_backups_need_a_login(app):
    with TestClient(app) as c:
        assert c.get("/backups", follow_redirects=False).status_code in (302, 303, 307, 401)


def test_backup_schedule(app, config):
    now = datetime(2026, 10, 23, 12, tzinfo=UTC)
    assert backup_due(now, "7", None)
    assert not backup_due(now, "7", (now - timedelta(days=6)).isoformat())
    assert backup_due(now, "7", (now - timedelta(days=7)).isoformat())
    assert not backup_due(now, "", None)  # "never"
    with app.state.session_factory() as db:
        settings_store.put(db, "backup.keep", "3")
        db.commit()
        folder = backup.backup_dir(config.config_dir)
        folder.mkdir(parents=True)
        for d in range(5):
            (folder / f"fumora-2020010{d + 1}-000000-scheduled.db").write_bytes(b"x")
        asyncio.run(scheduled_backup(db, now))
        assert settings_store.get_state(db, "schedule.backup.last") == now.isoformat()
    kinds = [b.label for b in backup.list_backups(folder)]
    assert kinds.count("scheduled") == 3
