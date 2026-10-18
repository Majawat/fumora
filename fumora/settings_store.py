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
    SettingDef("metron.username", "Username", "Metron"),
    SettingDef("metron.password", "Password", "Metron", secret=True),
    SettingDef(
        "comicvine.api_key",
        "API key",
        "ComicVine",
        secret=True,
        help="Fallback only, for series Metron does not have.",
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
        default="05:00",
        allow_empty=True,
        help="Fetches Metron's release schedule, refreshes series with new issues, then a batch "
        "of others. Reads only; it doesn't touch files. Leave empty to turn it off.",
    ),
    SettingDef(
        "schedule.refresh_batch",
        "Series rechecked per day",
        "Schedule",
        default="50",
        help="Besides series with releases this week. Ongoing series are rechecked weekly, ended ones "
        "monthly; each costs 2-3 Metron requests (5,000 a day allowed).",
    ),
]

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
    SettingDef("quality.preferred_groups", "Preferred groups", "Quality",
               help="Comma-separated, e.g. Empire, DCP. Scores higher; never required."),
    SettingDef("quality.blocked_groups", "Blocked groups", "Quality", help="Comma-separated; never grabbed."),
    SettingDef("quality.protocols", "Protocols, in order", "Quality", default="usenet, torrent",
               help="Remove one to never use it. Source ranking is fixed: digital, HD-Upscaled, then scans."),
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
    SettingDef("search.auto", "Search Wanted automatically", "Search", default="off",
               choices=(("off", "off (search by hand)"), ("on", "on"))),
    SettingDef("search.interval_hours", "Every (hours)", "Search", default="6"),
    SettingDef("search.delay_hours", "Wait after release (hours)", "Search", default="0",
               help="Give a digital release time to appear before settling for something else."),
    SettingDef("import.tag", "Tag usenet imports", "Search", default="on",
               choices=(("on", "on (confirmed series only)"), ("off", "off"))),
    SettingDef("import.tag_torrents", "Tag torrent imports", "Search", default="off",
               choices=(("off", "off (keeps the hardlink: no extra disk space while seeding)"),
                        ("on", "on (rewrites the file: a second copy while it seeds)"))),
]  # fmt: skip

SETTINGS_BY_KEY = {s.key: s for s in SETTINGS}


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
