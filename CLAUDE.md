# CLAUDE.md

Fumora: Metron-first comic manager (Mylar3 replacement). Python 3.12+,
FastAPI, SQLAlchemy 2 + Alembic on SQLite, Jinja2 + htmx (vendored in
`fumora/web/static`). One container, port 8792.

Design truth: the Claude Doc linked in `docs/DESIGN.md`. Read it first.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                                   # offline; no network calls
.venv/bin/ruff check . && .venv/bin/ruff format --check .   # CI runs both
FUMORA_CONFIG_DIR=./data/config .venv/bin/uvicorn fumora.asgi:app --reload --port 8792
.venv/bin/alembic revision --autogenerate -m "..."  # after editing models.py
docker compose up --build
```

## Rules

- Own integer ids everywhere; metron_id / cv_id / gcd_id are optional cross-reference columns.
- Field names follow Metron's API. A TPB is its own series (Metron series_type), linked via `reprint`.
- All Metron calls go through the single `app.state.metron_limiter` (burst 20, 5,000/day).
- Secrets live in the `setting` table and are never rendered back to the browser or logged.
- TRaSH Guides paths: one `/data` mount shared with the download clients; library default `/data/media/comics` (configurable). Import = hardlink (torrents) or rename/move (usenet), never copy across mounts.
- Never delete library files; never chown anything outside `/config`.
- Tests stay offline (respx mocks). `tests/test_migrations.py` fails if models and migrations drift.
- Files get ComicInfo.xml 2.1 + MetronInfo.xml 1.1, merged — never drop fields we don't own (e.g. `<Tags>`).
- No integration with Komga or BookOrbit; they folder-watch.
- Monitoring: `Issue.monitored` is the only stored choice; Wanted/Missing/Unreleased/Downloaded are derived (`monitoring.py`). `Issue.status` is Mylar's legacy value, display only.
- Metron budget: never page Metron's `modified_gt` change feed (can be 60k+ issues/day). Daily refresh = release schedule + rolling batch (`refresh.py`).
- Test instance (`fumora-test`, copies in `/data/media/comics-test`) is disposable. Production comic files are never written, renamed or converted without the owner's explicit OK.
