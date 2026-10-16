"""ComicInfo.xml (Anansi 2.1 draft) and MetronInfo.xml (1.1) writers.

ComicInfo is merged: fields Fumora has a value for are set, everything else
in the existing file is kept as it was (page info, scan info, fields we don't
own, unknown elements). <Tags> is a union of the file's tags and Fumora's.
MetronInfo is rebuilt from Fumora's data, keeping any existing <Tags>.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date

COMICINFO_ORDER = [
    "Title", "Series", "Number", "Count", "Volume", "AlternateSeries", "AlternateNumber",
    "AlternateCount", "Summary", "Notes", "Year", "Month", "Day", "Writer", "Penciller", "Inker",
    "Colorist", "Letterer", "CoverArtist", "Editor", "Translator", "Publisher", "Imprint", "Genre",
    "Tags", "Web", "PageCount", "LanguageISO", "Format", "BlackAndWhite", "Manga", "Characters",
    "Teams", "Locations", "ScanInformation", "StoryArc", "StoryArcNumber", "SeriesGroup",
    "AgeRating", "Pages", "CommunityRating", "MainCharacterOrTeam", "Review", "GTIN",
]  # fmt: skip

# Metron role -> ComicInfo credit field (ComicTagger's conventions).
ROLE_FIELDS = {
    "Writer": ["Writer"], "Script": ["Writer"], "Story": ["Writer"], "Plot": ["Writer"],
    "Artist": ["Penciller", "Inker"], "Illustrator": ["Penciller", "Inker"],
    "Penciller": ["Penciller"], "Breakdowns": ["Penciller"], "Layouts": ["Penciller"],
    "Inker": ["Inker"], "Finishes": ["Inker"], "Embellisher": ["Inker"],
    "Colorist": ["Colorist"], "Colors": ["Colorist"],
    "Letterer": ["Letterer"],
    "Cover": ["CoverArtist"],
    "Editor": ["Editor"],
    "Translator": ["Translator"],
}  # fmt: skip

# Metron series type -> ComicInfo <Format> (the strings readers already know).
FORMATS = {
    "Single Issue": "Series",
    "Limited Series": "Limited Series",
    "One-Shot": "One-Shot",
    "Annual": "Annual",
    "Trade Paperback": "TPB",
    "Hardcover": "HC",
    "Omnibus": "Omnibus",
    "Graphic Novel": "GN",
    "Digital Chapter": "Digital Chapter",
}

COLLECTED_TYPES = {"Trade Paperback", "Hardcover", "Omnibus", "Graphic Novel"}

METRON_ROLES = {
    "Writer", "Script", "Story", "Plot", "Interviewer", "Artist", "Penciller", "Breakdowns",
    "Illustrator", "Layouts", "Inker", "Embellisher", "Finishes", "Ink Assists", "Colorist",
    "Color Separations", "Color Assists", "Color Flats", "Digital Art Technician", "Gray Tone",
    "Letterer", "Cover", "Editor", "Consulting Editor", "Assistant Editor", "Associate Editor",
    "Group Editor", "Senior Editor", "Managing Editor", "Collection Editor", "Production",
    "Designer", "Logo Design", "Translator", "Supervising Editor", "Executive Editor",
    "Editor In Chief", "President", "Publisher", "Chief Creative Officer", "Executive Producer",
    "Other",
}  # fmt: skip

CURRENCY_COUNTRY = {"USD": "US", "CAD": "CA", "GBP": "GB", "EUR": "EU", "AUD": "AU", "JPY": "JP"}


@dataclass
class IssueMeta:
    """Everything Fumora knows about one issue, flattened for writing."""

    series_name: str
    number: str
    series_year: int | None = None
    series_sort_name: str | None = None
    series_volume: int | None = None
    series_type: str | None = None
    series_language: str | None = None
    series_metron_id: int | None = None
    publisher: str | None = None
    publisher_metron_id: int | None = None
    imprint: str | None = None
    imprint_metron_id: int | None = None
    genres: list[str] = field(default_factory=list)
    title: str | None = None
    stories: list[str] = field(default_factory=list)
    collection_title: str | None = None
    alt_number: str | None = None
    summary: str | None = None
    cover_date: date | None = None
    store_date: date | None = None
    page_count: int | None = None
    price: str | None = None
    price_currency: str | None = None
    isbn: str | None = None
    upc: str | None = None
    # [(creator, creator_metron_id, [(role, role_id), ...])]
    credits: list[tuple[str, int | None, list[tuple[str, int | None]]]] = field(default_factory=list)
    characters: list[tuple[str, int | None]] = field(default_factory=list)
    teams: list[tuple[str, int | None]] = field(default_factory=list)
    universes: list[tuple[str, int | None]] = field(default_factory=list)
    arcs: list[tuple[str, int | None]] = field(default_factory=list)
    reprints: list[tuple[str, int | None]] = field(default_factory=list)
    age_rating_metron: str | None = None
    age_rating_comicinfo: str | None = None
    tags: list[str] = field(default_factory=list)
    metron_id: int | None = None
    cv_id: int | None = None
    gcd_id: int | None = None
    web: str | None = None
    modified: str | None = None
    notes: str | None = None


def _join(values) -> str | None:
    out = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return ", ".join(out) or None


def _split(value: str | None) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def comicinfo_fields(meta: IssueMeta) -> dict[str, str]:
    """The ComicInfo fields Fumora sets, as text. Missing values are left out."""
    credit_fields: dict[str, list[str]] = {}
    for creator, _cid, roles in meta.credits:
        for role, _rid in roles:
            for f in ROLE_FIELDS.get(role, []):
                credit_fields.setdefault(f, []).append(creator)

    values = {
        "Title": meta.title or _join(meta.stories),
        "Series": meta.series_name,
        "Number": meta.number,
        "Volume": str(meta.series_year) if meta.series_year else None,
        "Summary": meta.summary,
        "Notes": meta.notes,
        "Year": str(meta.cover_date.year) if meta.cover_date else None,
        "Month": str(meta.cover_date.month) if meta.cover_date else None,
        "Day": str(meta.cover_date.day) if meta.cover_date else None,
        **{f: _join(names) for f, names in credit_fields.items()},
        "Publisher": meta.publisher,
        "Imprint": meta.imprint,
        "Genre": _join(meta.genres),
        "Web": meta.web,
        "LanguageISO": meta.series_language,
        "Format": FORMATS.get(meta.series_type or ""),
        "Characters": _join(n for n, _ in meta.characters),
        "Teams": _join(n for n, _ in meta.teams),
        "StoryArc": _join(n for n, _ in meta.arcs),
        "AgeRating": meta.age_rating_comicinfo,
        "GTIN": meta.isbn or meta.upc,
    }
    # XML parsing turns \r\n into \n; normalise so a re-read compares equal.
    cleaned = {k: v.replace("\r\n", "\n").replace("\r", "\n").strip() for k, v in values.items() if v}
    return {k: v for k, v in cleaned.items() if v}


def merge_comicinfo(existing: bytes | None, meta: IssueMeta) -> tuple[bytes, dict[str, tuple]]:
    """Return the new ComicInfo.xml and {field: (old, new)} for fields that change."""
    root = None
    if existing:
        try:
            root = ET.fromstring(existing)
        except ET.ParseError:
            root = None  # unreadable: start fresh (keeping nothing is the only option)
    if root is None or root.tag != "ComicInfo":
        root = ET.Element("ComicInfo")

    children: dict[str, ET.Element] = {}
    extras: list[ET.Element] = []
    for child in list(root):
        if child.tag in COMICINFO_ORDER and child.tag not in children:
            children[child.tag] = child
        else:
            extras.append(child)

    new = comicinfo_fields(meta)
    file_tags = _split(children["Tags"].text) if "Tags" in children else []
    merged_tags = _join(file_tags + meta.tags)
    if merged_tags:
        new["Tags"] = merged_tags

    changes: dict[str, tuple] = {}
    for name, value in new.items():
        old = children[name].text if name in children else None
        if (old or "").strip() != value:
            changes[name] = (old, value)
        el = children.get(name)
        if el is None:
            el = children[name] = ET.Element(name)
        el.text = value

    # The root only ever carries namespace declarations; write the standard pair.
    out = ET.Element(
        "ComicInfo",
        {
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xmlns:xsd": "http://www.w3.org/2001/XMLSchema",
        },
    )
    for name in COMICINFO_ORDER:
        if name in children:
            out.append(children[name])
    for el in extras:
        out.append(el)
    ET.indent(out, space="  ")
    return ET.tostring(out, encoding="utf-8", xml_declaration=True), changes


def _sub(parent: ET.Element, tag: str, text=None, **attrs) -> ET.Element:
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items() if v is not None})
    if text is not None:
        el.text = str(text)
    return el


def _resources(parent: ET.Element, wrapper: str, tag: str, items) -> None:
    items = [(n, i) for n, i in items if n]
    if items:
        w = _sub(parent, wrapper)
        for name, rid in items:
            _sub(w, tag, name, id=rid)


def build_metroninfo(meta: IssueMeta, existing: bytes | None = None) -> bytes:
    """MetronInfo.xml from Fumora's data; <Tags> already in the file are kept."""
    file_tags: list[str] = []
    if existing:
        try:
            old = ET.fromstring(existing)
            file_tags = [t.text.strip() for t in old.findall("./Tags/Tag") if t.text and t.text.strip()]
        except ET.ParseError:
            pass

    root = ET.Element(
        "MetronInfo",
        {
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xmlns:xsd": "http://www.w3.org/2001/XMLSchema",
        },
    )
    ids = [("Metron", meta.metron_id), ("Comic Vine", meta.cv_id), ("Grand Comics Database", meta.gcd_id)]
    ids = [(src, v) for src, v in ids if v]
    if ids:
        w = _sub(root, "IDS")
        for n, (src, value) in enumerate(ids):
            _sub(w, "ID", value, source=src, primary="true" if n == 0 else None)
    if meta.publisher:
        pub = _sub(root, "Publisher", id=meta.publisher_metron_id)
        _sub(pub, "Name", meta.publisher)
        if meta.imprint:
            _sub(pub, "Imprint", meta.imprint, id=meta.imprint_metron_id)
    series = _sub(root, "Series", id=meta.series_metron_id, lang=meta.series_language)
    _sub(series, "Name", meta.series_name)
    if meta.series_sort_name:
        _sub(series, "SortName", meta.series_sort_name)
    if meta.series_volume:
        _sub(series, "Volume", meta.series_volume)
    if meta.series_type in FORMATS:
        _sub(series, "Format", meta.series_type)
    if meta.series_year:
        _sub(series, "StartYear", meta.series_year)
    if meta.collection_title:
        _sub(root, "CollectionTitle", meta.collection_title)
    _sub(root, "Number", meta.number)
    if meta.alt_number:
        _sub(root, "AlternativeNumber", meta.alt_number)
    _resources(root, "Stories", "Story", [(s, None) for s in meta.stories])
    if meta.summary:
        _sub(root, "Summary", meta.summary)
    country = CURRENCY_COUNTRY.get(meta.price_currency or "")
    if meta.price and country:
        _sub(_sub(root, "Prices"), "Price", meta.price, country=country)
    if meta.cover_date:
        _sub(root, "CoverDate", meta.cover_date.isoformat())
    if meta.store_date:
        _sub(root, "StoreDate", meta.store_date.isoformat())
    if meta.page_count:
        _sub(root, "PageCount", meta.page_count)
    if meta.notes:
        _sub(root, "Notes", meta.notes)
    _resources(root, "Genres", "Genre", [(g, None) for g in meta.genres])
    tags = []
    for t in file_tags + meta.tags:
        if t not in tags:
            tags.append(t)
    _resources(root, "Tags", "Tag", [(t, None) for t in tags])
    if meta.arcs:
        w = _sub(root, "Arcs")
        for name, rid in meta.arcs:
            _sub(_sub(w, "Arc", id=rid), "Name", name)
    _resources(root, "Characters", "Character", meta.characters)
    _resources(root, "Teams", "Team", meta.teams)
    if meta.universes:
        w = _sub(root, "Universes")
        for name, rid in meta.universes:
            _sub(_sub(w, "Universe", id=rid), "Name", name)
    if meta.series_type in COLLECTED_TYPES:
        _resources(root, "Reprints", "Reprint", meta.reprints)
    if meta.isbn or meta.upc:
        gtin = _sub(root, "GTIN")
        if meta.isbn:
            _sub(gtin, "ISBN", meta.isbn)
        if meta.upc:
            _sub(gtin, "UPC", meta.upc)
    if meta.age_rating_metron:
        _sub(root, "AgeRating", meta.age_rating_metron)
    if meta.web:
        _sub(_sub(root, "URLs"), "URL", meta.web, primary="true")
    if meta.credits:
        w = _sub(root, "Credits")
        for creator, cid, roles in meta.credits:
            credit = _sub(w, "Credit")
            _sub(credit, "Creator", creator, id=cid)
            if roles:
                rw = _sub(credit, "Roles")
                for role, rid in roles:
                    if role in METRON_ROLES:
                        _sub(rw, "Role", role, id=rid)
                    else:
                        _sub(rw, "Role", "Other")
    if meta.modified:
        _sub(root, "LastModified", meta.modified)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
