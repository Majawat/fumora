"""User-editable settings stored in the `setting` table.

Secret values (passwords, API keys) are write-only from the UI's point of
view: the page shows whether one is set, never the value.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from .models import Setting


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    group: str
    secret: bool = False
    default: str | None = None
    help: str = ""
    choices: tuple[tuple[str, str], ...] = ()
    # An empty value is a real choice (e.g. "off"), not "use the default".
    allow_empty: bool = False


SETTINGS: list[SettingDef] = [
    SettingDef(
        "metron.api_token",
        "API token",
        "Metron",
        secret=True,
        help="Recommended. On metron.cloud: your profile, API Tokens, Generate Token (shown once). "
        "Used instead of the username and password, which Metron is phasing out for apps.",
    ),
    SettingDef("metron.username", "Username", "Metron", help="Only needed without an API token."),
    SettingDef("metron.password", "Password", "Metron", secret=True),
    SettingDef(
        "comicvine.api_key",
        "API key",
        "ComicVine",
        secret=True,
        help="For series Metron doesn't have (manga, some small press): their issue lists are kept "
        "current from ComicVine until Metron has them. Mylar's key works.",
    ),
    SettingDef(
        "library.path",
        "Library folder",
        "Library",
        default="/data/media/comics",
        help="Where series folders live, as Fumora sees it. Mount one shared /data "
        "(TRaSH layout) so imports can hardlink or move instantly.",
    ),
    SettingDef(
        "library.recycle_path",
        "Recycle folder",
        "Library",
        default="/data/media/fumora-recycle",
        help="Originals Fumora replaces (e.g. a CBR after converting it to CBZ) are moved here, "
        "never deleted. Keep it outside folders Komga/Kavita/BookOrbit watch.",
    ),
    SettingDef(
        "mylar.db_path",
        "Mylar database",
        "Mylar import",
        default="/mylar/mylar/mylar.db",
        help="Path to Mylar's mylar.db. Mount Mylar's appdata folder read-only at /mylar. "
        "Fumora only reads a copy of it.",
    ),
]

# Naming (Sonarr-style templates). Defaults reproduce Mylar's scheme.
SETTINGS += [
    SettingDef(
        "naming.folder_template",
        "Series folder",
        "Naming",
        default="{Series Title} ({Series Year})",
        help="Tokens: {Series Title} {Series TitleThe} {Series Year} {Volume} {Publisher} "
        "{Series Type} {Format}. A / makes subfolders, e.g. {Publisher}/{Series Title} ({Series Year}).",
    ),
    SettingDef(
        "naming.file_template",
        "Issue file",
        "Naming",
        default="{Series Title} {Issue:000} ({Issue Year})",
        help="Also: {Issue} {Issue:000} {Issue Title} {Issue Year} {Issue Month:00} {Metron Id}. "
        "The file extension is added automatically.",
    ),
    SettingDef(
        "naming.collected_template",
        "Collected editions file (optional)",
        "Naming",
        help="Used for TPB, hardcover, omnibus and graphic novel series instead of the issue template.",
    ),
    SettingDef(
        "naming.colon",
        "Colons become",
        "Naming",
        default="delete",
        choices=(
            ("delete", "nothing (Mylar)"),
            ("dash", "-"),
            ("space_dash", " -"),
            ("space_dash_space", " - "),
        ),
    ),
]

SETTINGS += [
    SettingDef(
        "schedule.refresh_time",
        "Daily Metron refresh (UTC, HH:MM)",
        "Schedule",
        allow_empty=True,
        help="Fetches Metron's release schedule, refreshes series with new issues, then a batch "
        "of others. Reads only; it doesn't touch files. Picked at random on first start, so "
        "installs don't all hit Metron at the same moment (Metron's request). "
        "Leave empty to turn it off.",
    ),
    SettingDef(
        "schedule.refresh_batch",
        "Series rechecked per day",
        "Schedule",
        default="50",
        help="Besides series with releases this week. Ongoing series are rechecked weekly, ended ones "
        "monthly; each costs 2-3 Metron requests (5,000 a day standard; Metron supporters get more).",
    ),
]

SETTINGS += [
    SettingDef("schedule.cv_refresh_time", "Daily ComicVine refresh (UTC, HH:MM)", "Schedule",
               allow_empty=True,
               help="Series Metron doesn't have. Picked at random on first start. Leave empty for off."),
    SettingDef("schedule.cv_refresh_batch", "ComicVine series rechecked per day", "Schedule", default="30",
               help="Monitored ones weekly, others monthly; one request each, about 20 s apart."),
]  # fmt: skip

SETTINGS += [
    SettingDef("prowlarr.url", "URL", "Indexers (Prowlarr)", help="e.g. http://192.168.9.10:9696"),
    SettingDef("prowlarr.api_key", "API key", "Indexers (Prowlarr)", secret=True),
    SettingDef("quality.min_mb", "Smallest single issue (MB)", "Quality", default="5",
               help="Smaller releases are usually fakes or previews."),
    SettingDef("quality.max_mb", "Largest single issue (MB)", "Quality", default="300"),
    SettingDef("quality.max_collected_mb", "Largest collected edition or pack (MB)", "Quality",
               default="2048"),
    SettingDef("quality.ignore_words", "Ignore releases containing", "Quality",
               default=".exe, .iso, pdf-xpost, preview, sample", allow_empty=True,
               help="Comma-separated. Ebooks (EPUB, AZW3...) are recognised and skipped anyway."),
    SettingDef("search.languages", "Languages", "Quality", default="en",
               help="Comma-separated codes, e.g. en or en, fr. Releases in these are grabbed normally."),
    SettingDef("search.other_languages", "Other languages", "Quality", default="ask",
               choices=(("ask", "ask me (held on the Queue page until you OK it)"),
                        ("reject", "never grab"))),
    SettingDef("quality.preferred_groups", "Preferred groups", "Quality",
               help="Comma-separated, e.g. Empire, DCP. Scores higher; never required."),
    SettingDef("quality.blocked_groups", "Blocked groups", "Quality", help="Comma-separated; never grabbed."),
    SettingDef("quality.protocols", "Protocols, in order", "Quality", default="usenet, torrent, ddl",
               help="ddl = direct downloads (GetComics, Anna's Archive). Remove one to never use it. "
               "Source ranking is fixed: digital, HD-Upscaled, then scans."),
]  # fmt: skip

SETTINGS += [
    SettingDef("sabnzbd.url", "URL", "Usenet (SABnzbd)", help="e.g. http://192.168.9.10:8085"),
    SettingDef("sabnzbd.api_key", "API key", "Usenet (SABnzbd)", secret=True),
    SettingDef("sabnzbd.category", "Category", "Usenet (SABnzbd)", default="fumora",
               help="A category of Fumora's own, so Mylar never picks up its downloads."),
    SettingDef("qbittorrent.url", "URL", "Torrents (qBittorrent)", help="e.g. http://192.168.9.10:8100"),
    SettingDef("qbittorrent.username", "Username", "Torrents (qBittorrent)"),
    SettingDef("qbittorrent.password", "Password", "Torrents (qBittorrent)", secret=True),
    SettingDef("qbittorrent.category", "Category", "Torrents (qBittorrent)", default="fumora",
               help="Torrents are hardlinked into the library and keep seeding."),
    SettingDef("getcomics.enabled", "Search GetComics", "Direct downloads (GetComics)", default="off",
               choices=(("off", "off"), ("on", "on"))),
    SettingDef("getcomics.url", "URL", "Direct downloads (GetComics)", default="https://getcomics.org"),
    SettingDef("flaresolverr.url", "FlareSolverr / byparr URL (optional)", "Direct downloads (GetComics)",
               help="Only used when GetComics shows a Cloudflare challenge, e.g. http://192.168.9.10:8191"),
    SettingDef("ddl.path", "Download folder", "Direct downloads (GetComics)",
               default="/data/downloads/fumora",
               help="Fumora downloads GetComics files here itself (main server, then Pixeldrain). "
               "Keep it on the same /data mount as the library so imports are a rename."),
    SettingDef("shelfmark.url", "URL", "Anna's Archive (Shelfmark)",
               help="e.g. http://192.168.9.10:8084. Shelfmark searches Anna's Archive and downloads; "
               "Fumora imports the file from Shelfmark's folder (same /data mount)."),
    SettingDef("shelfmark.api_key", "API key (optional)", "Anna's Archive (Shelfmark)", secret=True,
               help="Only if Shelfmark has a login: its SHELFMARK_API_KEY."),
    SettingDef("shelfmark.sources", "Shelfmark sources", "Anna's Archive (Shelfmark)",
               default="direct_download",
               help="direct_download = Anna's Archive. Add libgen if Shelfmark's Libgen search is on "
               "(it has more CBZ/CBR comics)."),
    SettingDef("search.auto", "Search Wanted automatically", "Search", default="off",
               choices=(("off", "off (search by hand)"), ("on", "on"))),
    SettingDef("search.interval_hours", "Every (hours)", "Search", default="6"),
    SettingDef("search.delay_hours", "Wait after release (hours)", "Search", default="0",
               help="Give a digital release time to appear before settling for something else."),
    SettingDef("search.retry_failed", "When a download fails", "Search", default="on",
               choices=(("on", "search again for that issue (up to 5 times a day)"), ("off", "do nothing"))),
    SettingDef("import.roots", "Folders you can import files from", "Search", default="/data",
               help="Comma-separated. The Import files page only reads and moves files under these."),
    SettingDef("import.tag", "Tag usenet and direct-download imports", "Search", default="on",
               choices=(("on", "on (confirmed series only)"), ("off", "off"))),
    SettingDef("import.tag_torrents", "Tag torrent imports", "Search", default="off",
               choices=(("off", "off (keeps the hardlink: no extra disk space while seeding)"),
                        ("on", "on (rewrites the file: a second copy while it seeds)"))),
]  # fmt: skip

SETTINGS_BY_KEY = {s.key: s for s in SETTINGS}


def metron_login(db: Session) -> tuple[str | None, str | None, str | None]:
    """(username, password, token) for Metron; usable if there's a token or both of the others."""
    return get(db, "metron.username"), get(db, "metron.password"), get(db, "metron.api_token")


def metron_usable(user: str | None, password: str | None, token: str | None) -> bool:
    """A token, or a username with its password (until Metron drops those)."""
    return bool(token or (user and password))


def metron_ready(db: Session) -> bool:
    return metron_usable(*metron_login(db))


def get_state(db: Session, key: str) -> str | None:
    """Internal state (e.g. when the last refresh ran); not shown on the Settings page."""
    row = db.get(Setting, f"state.{key}")
    return row.value if row else None


def put_state(db: Session, key: str, value: str | None) -> None:
    row = db.get(Setting, f"state.{key}")
    if row is None:
        db.add(Setting(key=f"state.{key}", value=value))
    else:
        row.value = value


def get(db: Session, key: str) -> str | None:
    row = db.get(Setting, key)
    sdef = SETTINGS_BY_KEY.get(key)
    if row is not None and row.value == "" and sdef is not None and sdef.allow_empty:
        return ""
    if row is not None and row.value not in (None, ""):
        return row.value
    return SETTINGS_BY_KEY[key].default if key in SETTINGS_BY_KEY else None


def put(db: Session, key: str, value: str | None) -> None:
    if key not in SETTINGS_BY_KEY:
        raise KeyError(key)
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def is_set(db: Session, key: str) -> bool:
    row = db.get(Setting, key)
    return row is not None and row.value not in (None, "")


def apply_form(db: Session, form: dict[str, str]) -> list[str]:
    """Save submitted settings; returns the keys that changed.

    A blank secret field means "keep the saved value", so the page never has
    to send a secret back to the browser.
    """
    changed = []
    for sdef in SETTINGS:
        if sdef.key not in form:
            continue
        value = form[sdef.key].strip()
        if sdef.secret and value == "":
            if form.get(f"{sdef.key}.clear") and is_set(db, sdef.key):
                put(db, sdef.key, None)
                changed.append(sdef.key)
            continue
        if value != (get(db, sdef.key) or ""):
            put(db, sdef.key, value if (value or sdef.allow_empty) else None)
            changed.append(sdef.key)
    return changed
