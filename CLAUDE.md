# CLAUDE.md

Fumora: Metron-first comic manager (Mylar3 replacement). Python 3.12+,
FastAPI, SQLAlchemy 2 + Alembic on SQLite, Jinja2 + htmx (vendored in
`fumora/web/static`). One container, port 8792.

Design truth: the Claude Doc linked in `docs/DESIGN.md`. Current state and next steps: `docs/HANDOFF.md`. Read both first.

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
- Library files are deleted only by the owner's own action in the UI (Delete series with "also delete its
  files" ticked); replaced originals and deleted files go to the recycle folder unless the owner turned it off
  (`library.recycle`, see `recycle.py`). Fumora never decides by itself to delete. Never chown anything outside `/config`.
- Tests stay offline (respx mocks). `tests/test_migrations.py` fails if models and migrations drift.
- Files get ComicInfo.xml 2.1 + MetronInfo.xml 1.1, merged — never drop fields we don't own (e.g. `<Tags>`).
- No integration with Komga or BookOrbit; they folder-watch.
- Monitoring: `Issue.monitored` is the only stored choice; Wanted/Missing/Unreleased/Downloaded are derived (`monitoring.py`). `Issue.status` is Mylar's legacy value, display only.
- Metron budget: never page Metron's `modified_gt` change feed (can be 60k+ issues/day). Daily refresh = release schedule + rolling batch (`refresh.py`).
- Unraid permissions: Fumora runs as PUID:PGID (99:100) with UMASK 0002 and never as root. Everything it
  creates, moves in, unpacks or converts ends up 664/775 (`perms.normalize`); hardlinked torrent files are
  left alone. `tests/container/smoke.sh` (CI job `container-smoke`) checks the real image. Ad-hoc scripts in
  the containers run as `docker exec -u 99:100 <c> sh -c 'umask 0002; ...'`.
- No silent fallbacks: a missing or read-only folder, a failed login or an invalid setting is said out loud
  (dashboard notice + log), never quietly worked around. Every setting must do what its label says, with a test.
- Before debugging a third-party app, read the source of the version that is deployed, not main.
- Test instance (`fumora-test`, copies in `/data/media/comics-test`) is disposable. Production comic files are never written, renamed or converted without the owner's explicit OK.
