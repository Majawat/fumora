# Fumora — handoff

State as of 2026-10-19. Read this, `CLAUDE.md` (coding rules) and the design doc
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
| M4 part 2: grab, download monitor, import | merged 2026-10-18 | #25 |
| PDF → CBZ (extract page images or render at 300 dpi) | merged 2026-10-18 | #26 |
| M5a GetComics + Anna's Archive (via Shelfmark) | on test, branch `m5-ddl`, no PR yet | — |
| M5 rest: TPB links, Metron gaps page, GCD ratings, full tags at import (#24) | not started | — |
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
- Private Dance #1 came from the owner as a PDF (2026-10-18): converted to
  `~/claude/Comic-Manager/ready/Private Dance 001 (2023).cbz` (36 pages,
  1991x3060) and placed in the **test** library only; copying it into the
  production Comics folder was blocked by auto mode and needs the owner (SMB
  copy) or an explicit go-ahead.
- Private Dance #1–4 have no single-issue releases; they only exist in
  `Private.Dance.Vol.01-Season.One...TPB` (relevant to #5).
- Imports get basic tags only; full Metron tags at import is #24.
- qBittorrent: no credentials yet (none in the environment). Owner can enter
  them in Fumora's Settings. MyAnonamouse is a private tracker: torrents must
  be hardlinked and keep seeding; tagging torrent imports is off by default
  because rewriting the file breaks the hardlink.

**Next:** production needs `/data` read-write
(owner's OK) before it can import or write tags; then M5.

### Where M5a (direct downloads) stands

- `getcomics.py`: own GetComics indexer. Search page → posts whose title is for
  the wanted series/issues (`search.wants`) → post page → one release per
  group (name + size line, then host buttons). Titles come from the real file
  name in a host link when there is one (gives source + group). Paced 1 req/s,
  ≤ 8 posts per search; FlareSolverr/byparr only if Cloudflare challenges.
- `ddl.py`: Fumora downloads GetComics files itself (main server, then
  Pixeldrain; other hosts skipped), into `ddl.path/<job>/`, unpacks packs of
  CBZ/CBR. Jobs are in memory; after a restart the monitor restarts them.
  Remove / Try another cancels a running one.
- `shelfmark.py`: Anna's Archive via Shelfmark's API (`/api/releases`,
  `/api/releases/download`, `/api/status`); file imported from Shelfmark's
  folder. Shelfmark on Hathor has AUTH_METHOD=none, so no key needed.
- `indexers.py`: Prowlarr + GetComics + Shelfmark behind one search call; a
  failing source is reported, the others still count. New protocol `ddl`
  (migration 0007 adds it to a saved protocol list).
- **Live-tested 2026-10-19 on fumora-test** (local image `fumora:m5`):
  Absolute Batman #21-23 grabbed from GetComics, downloaded (~0.5 MB/s from
  the main server), imported, tagged, job folder removed; #22 went through the
  restart path. Test settings: `getcomics.enabled=on`,
  `ddl.path=/data/usenet/fumora-ddl` (same mount as the test library),
  `flaresolverr.url=http://192.168.9.10:8191`, `shelfmark.url=http://192.168.9.10:8084`.
  Helper: `/mnt/user/appdata/fumora-test/m5run.py "<series>" <number> [getcomics]`
  (run with `docker exec -u 99:100`; the app's own monitor does the download).
- **Also on the branch (2026-10-19, owner asks):**
  - Metron API token (`metron.api_token`, Bearer; username/password still a
    fallback until Metron drops Basic auth). Owner must generate a token on
    metron.cloud and save it in prod + test Settings.
  - Daily Metron refresh time is random per install (Metron asked: no fixed
    default), saved on first start with an Event; prod will move off 05:00 UTC
    on upgrade. 429/outage waits get up to 20% random jitter. The Metron test
    shows the account's `X-RateLimit-Sustained-Limit` (supporter tiers).
  - Languages: `search.languages` (default en), `search.other_languages`
    ask|reject. "ask" holds the best other-language release on the Queue page
    ("Needs your OK": OK / Reject); a later preferred release supersedes it.
    Interactive search lists them under "Other languages" with Grab anyway.
  - Numberless graphic novels count as #1 only for a one-issue collected/GN
    series, exact name, not an ebook, and a comic archive format or matching year.
  - Libgen titles cleaned (`shelfmark.clean_libgen_title`); Libgen also runs
    in issue-specific fallback searches (Anna's Archive doesn't: slow).
  - Failed downloads start a new search for their issues (`search.retry_failed`,
    ≤ 5 a day per issue). Needed because many Libgen copies are dead on its CDN
    (Black Hole #1: 5 of 6; #3: 7 of 7).
- **Live Shelfmark/Libgen test 2026-10-19:** Black Hole #1 grabbed via
  Shelfmark (Libgen), CBR converted, imported to
  `/data/Shelfmark/fumora-test-library/Black Hole (1995)/Black Hole 001 (1995).cbz`.
  fumora-test now also mounts `/mnt/user/data/Shelfmark:/data/Shelfmark:rw`;
  test `shelfmark.sources=libgen`. Test DB: Black Hole #1/#3 file rows were
  dropped to make them wanted (#3 has no live Libgen copy).
- **Manga (series not on Metron, e.g. Mieruko-chan, from Mylar with CV ids):**
  `Series.numbering` auto|issues|volumes (migration 0008); auto = volumes when
  issues are titled "Volume N" (ComicVine's manga naming). Volume numbering
  matches "Vol. 12" to issue 12 and uses the collected size limit. Parser now
  strips "Publisher - Series" prefixes (Libgen's "Yen.Press.-.Mieruko..."),
  Libgen folder paths are dropped. Live 2026-10-19: auto search for
  Mieruko-chan v12 on test grabbed Libgen's copy (dead on the CDN), the monitor
  re-searched by itself; only other copy is MyAnonamouse (torrent, can't import
  on fumora-test's layout). Mieruko-chan was copied into the test DB (id 15,
  only #12 monitored). GAP: not-on-Metron series never get new issues (refresh
  is Metron-only); plan = ComicVine fallback refresh by cv_id.
- **Shelfmark's Anna's Archive search is broken on Shelfmark's side** (v1.4.0):
  owner removed the bad `annas-archive.is` mirror; AA's search now answers with a
  DDoS-Guard CAPTCHA no bypasser can solve (byparr update didn't help; Shelfmark
  has no manual cookie import). Owner turned Shelfmark's Libgen search on: works.

## Deployments (Hathor, 192.168.9.10)

| Container | Port | Image | Notes |
| --- | --- | --- | --- |
| `fumora` (production) | 8792 | `ghcr.io/majawat/fumora:edge` | `/data` mounted **read-only**; scheduler on (daily Metron refresh 05:00 UTC) |
| `fumora-test` | 8793 | local `fumora:m5` (branch `m5-ddl`) | `FUMORA_SCHEDULER=0`; writable only `/data/media/comics-test` and `/data/usenet` (downloads + `fumora-test-library`) |

Unraid templates: `/boot/config/plugins/dockerMan/templates-user/my-fumora.xml`
and `my-fumora-test.xml`. Recreate containers with the same `docker run`
arguments (see git history of this doc or `docker inspect`).

Production DB backups before migrations:
`/mnt/user/appdata/fumora/fumora-backup-pre-m3-2026-10-17.db`,
`/mnt/user/appdata/fumora/fumora-backup-pre-m4-2026-10-18.db` (schema now 0006).

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
  Archive (M5; see "Where M5a stands": Shelfmark's AA search is broken) · #8 expose externally? · #14 Metron gaps / contribution helper
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
