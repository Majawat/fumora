# Fumora

A Metron-first comic manager: it tracks series, finds and downloads issues and
collected editions, and writes clean, consistently named files with full
ComicInfo.xml and MetronInfo.xml. Reading is left to Komga, Kavita or
BookOrbit, which simply watch the library folder.

The name comes from Italian *fumetti* ("little puffs of smoke"), the speech
balloons that gave comics their Italian name.

**Status:** early development (milestone M0: skeleton).

## Run

```bash
docker compose up --build
```

Open http://localhost:8792, create the admin account, then enter your Metron
login on the Settings page.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PUID` / `PGID` | 99 / 100 | User and group Fumora runs as |
| `UMASK` | 0002 | Permissions for files Fumora writes |
| `FUMORA_PORT` | 8792 | Web port |
| `FUMORA_CONFIG_DIR` | /config | Database and session key |

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
FUMORA_CONFIG_DIR=./data/config .venv/bin/uvicorn fumora.asgi:app --reload --port 8792
```

Schema changes: edit `fumora/models.py`, then
`.venv/bin/alembic revision --autogenerate -m "what changed"`. The app applies
migrations on startup.

Design: see [docs/DESIGN.md](docs/DESIGN.md).
