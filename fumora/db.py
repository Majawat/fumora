import os
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import sessionmaker

from .perms import normalize

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _prepare_file(db_url: str) -> None:
    """SQLite creates a new database (and its -wal/-shm files, which copy the
    database's mode) as 0644 whatever the umask, so the users group couldn't
    write it. Create it ourselves first, and correct an existing one."""
    path = make_url(db_url).database
    if not path or path == ":memory:" or not db_url.startswith("sqlite"):
        return
    db = Path(path)
    db.parent.mkdir(parents=True, exist_ok=True)
    if not db.exists():
        os.close(os.open(db, os.O_CREAT | os.O_WRONLY, 0o666))  # 0666 minus the umask
    for f in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        if f.exists() and f.stat().st_uid == os.getuid():
            normalize(f)


def make_engine(db_url: str) -> Engine:
    _prepare_file(db_url)
    engine = create_engine(db_url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


def upgrade_db(db_url: str) -> None:
    """Bring the database schema up to the latest migration."""
    _prepare_file(db_url)
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
