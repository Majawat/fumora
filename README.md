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
| `FUMORA_TRUSTED_PROXIES` | 127.0.0.1 | Reverse proxies whose `X-Forwarded-*` headers are believed (comma-separated addresses or CIDR ranges) |

### Behind a reverse proxy

Fumora runs on its own subdomain (e.g. `comics.example.com`) behind a proxy such
as Nginx Proxy Manager that ends https and forwards to `http://<host>:8792`.
Set `FUMORA_TRUSTED_PROXIES` to the proxy's address so Fumora sees the
visitor's address and https; until then the dashboard says that an untrusted
proxy is in front. The proxy must pass the `Host` header through (NPM does).
There is one admin account; after 5 wrong passwords from one address in 15
minutes, logins from it wait 15 minutes. Form posts from other sites are
refused (cross-site request forgery). Serving Fumora under a sub-path is not
supported.

### Paths (TRaSH Guides layout)

Mount your whole data share once, at `/data`, exactly as Sonarr, SABnzbd and
qBittorrent see it (`/mnt/user/data:/data` on Unraid). Fumora then sees
download folders (`/data/usenet/...`, `/data/torrents/...`) and the library
(`/data/media/comics` by default, changeable in Settings) on one filesystem,
so imports are hardlinks for torrents and instant moves for usenet, never
slow copies. Separate `/comics` and `/downloads` mounts would break that.

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

## Licence

GPL-3.0. See [LICENSE](LICENSE).
