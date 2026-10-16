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
        "{Series Type} {Format}",
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

SETTINGS_BY_KEY = {s.key: s for s in SETTINGS}


def get(db: Session, key: str) -> str | None:
    row = db.get(Setting, key)
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
            put(db, sdef.key, value or None)
            changed.append(sdef.key)
    return changed
