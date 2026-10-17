# Fumora — handoff

State as of 2026-10-17. Read this, `CLAUDE.md` (coding rules) and the design doc
(link in `docs/DESIGN.md`) before continuing.

## What Fumora is

A Metron-first comic manager replacing Mylar3 on the owner's Unraid server
(Hathor). It tracks series, finds and downloads issues and collected editions,
and writes clean files (ComicInfo.xml 2.1 + MetronInfo.xml 1.1). Komga, Kavita
and BookOrbit only folder-watch the library; Fumora never talks to them.

Repo: https://github.com/Majawat/fumora (public, GPL-3.0). Local checkout on
Bastet: `~/claude/fumora`. Python 3.14, FastAPI, SQLAlchemy + Alembic (SQLite),
Jinja2 + htmx. Image: `ghcr.io/majawat/fumora:edge` (built from `main`).

## Milestones

| Milestone | State | PR |
| --- | --- | --- |
| M0 skeleton | merged | — |
| M1 Mylar import + Metron match | merged | #10 |
| M2 tag writer, confirmation, scan, naming | merged | #17 |
| CBR/CB7/CBT → CBZ | merged | #18 |
| M2.1 library page, jobs page, issue edits | merged | #20 |
| M3 monitoring, daily refresh, releases, add series | merged | #21 |
| Metron rate limiting (after Metron's warning) | merged | #22 |
| M4 part 1: release parser, Prowlarr search, decisions | merged | #23 |
| **M4 part 2: grab, download monitor, import** | **PR open, awaiting owner merge** | #25 |
| M5 GetComics, Anna's Archive (via Shelfmark), TPB links, Metron gaps page, GCD ratings | not started | — |
| M6 side-by-side week with Mylar, cut over | not started | — |

### Where M4 part 2 stands

- Code done, code-reviewed (10 findings fixed), 182 offline tests pass.
- **Live-tested 2026-10-17 on the test instance:** *Absolute Batman #24* was
  searched, sent to SABnzbd (category `fumora`), downloaded, imported as
  `Absolute Batman 024 (2026).cbz`, tagged, and the job folder cleaned up.
  The first attempt correctly refused to import across two separate mounts
  (the test container's layout); the test container now mounts
  `/mnt/user/data/usenet` read-write and its *Absolute Batman* series folder
  is `/data/usenet/fumora-test-library/...` so download and library share a
  mount. Production mounts `/data` once and is unaffected.
- Private Dance #1–4 have no single-issue releases; they only exist in
  `Private.Dance.Vol.01-Season.One...TPB` (relevant to #5).
- Imports get basic tags only; full Metron tags at import is #24.
- qBittorrent: no credentials yet (none in the environment). Owner can enter
  them in Fumora's Settings. MyAnonamouse is a private tracker: torrents must
  be hardlinked and keep seeding; tagging torrent imports is off by default
  because rewriting the file breaks the hardlink.

**Next:** owner merges M4 part 2; then production needs `/data` read-write
(owner's OK) before it can import or write tags; then M5.

## Deployments (Hathor, 192.168.9.10)

| Container | Port | Image | Notes |
| --- | --- | --- | --- |
| `fumora` (production) | 8792 | `ghcr.io/majawat/fumora:edge` | `/data` mounted **read-only**; scheduler on (daily Metron refresh 05:00 UTC) |
| `fumora-test` | 8793 | local `fumora:m4b` | `FUMORA_SCHEDULER=0`; writable only `/data/media/comics-test` and `/data/usenet` (downloads + `fumora-test-library`) |

Unraid templates: `/boot/config/plugins/dockerMan/templates-user/my-fumora.xml`
and `my-fumora-test.xml`. Recreate containers with the same `docker run`
arguments (see git history of this doc or `docker inspect`).

Production DB backup before M3 migration:
`/mnt/user/appdata/fumora/fumora-backup-pre-m3-2026-10-17.db`.

## Rules that must not be broken

- **Production comic files** (`/mnt/user/data/media/Comics`) are never written,
  renamed or converted without the owner's explicit OK. Production `/data` is
  still read-only; switching it to read-write needs that OK (it is needed for
  production tag writes and imports).
- The test instance and `/mnt/user/data/media/comics-test` are disposable.
- **Metron:** the account was warned on 2026-10-17 (204 rate-limited responses
  in a day; access can be revoked). Only one Metron client at a time (no
  ad-hoc scripts while a Fumora job runs), ≤ 20 requests/minute, a 429 blocks
  every request for Retry-After, never page Metron's `modified_gt` feed.
- Merging a PR needs the owner's OK each time.
- Secrets live in Fumora's settings table, never rendered or logged. Prowlarr
  download links contain its API key: never show or log them.

## Production data (owner is fixing it)

- 621 series; 445 matched to Metron; 16 matches need confirming (Import page);
  159 not on Metron. Recommendations given: confirm 14, mark *Trillium* "not on
  Metron" (Metron's *Harbinger: Omegas* carries Trillium's ComicVine id),
  Uncle Scrooge is the owner's call.
- Monitoring after M3: 25 Wanted, 18 Missing.
- First library-wide tag preview (~8,100 Metron requests, ~2 days) waits for
  the owner to finish confirming; the real write needs `/data` read-write and
  should run overnight.

## Open issues worth knowing

- #5 does a TPB satisfy single issues (M5) · #7 Shelfmark test for Anna's
  Archive (M5) · #8 expose externally? · #14 Metron gaps / contribution helper
  · #16 parser (part done in M4 part 1).
- Not built yet from the M3 plan: one Fumora series spanning several Metron
  series (Uncle Scrooge), Metron pull-list sync (see #19 comments).

## How to work on it

```bash
cd ~/claude/fumora
.venv/bin/pytest -q                                   # offline; needs bsdtar for archive tests
.venv/bin/ruff check . && .venv/bin/ruff format --check .
docker build -t fumora:dev .                          # then docker save | ssh hathor docker load for the test instance
```

Run a code review (`/code-review medium <branch>`) before each PR; every
milestone so far had ~10 real findings.
