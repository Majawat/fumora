"""Database models.

Every row has its own integer id. Metron, ComicVine and GCD ids are optional
cross-reference columns, so a series that exists only on ComicVine still fits
and a Metron re-id is a one-column update. Field names follow Metron's API.
"""

import re
from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


_VOLUME_TITLE = re.compile(r"^\s*vol(?:ume)?\.?\s*\d+\s*$", re.I)


class Base(DeclarativeBase):
    pass


class Publisher(Base):
    __tablename__ = "publisher"

    id: Mapped[int] = mapped_column(primary_key=True)
    metron_id: Mapped[int | None] = mapped_column(unique=True)
    cv_id: Mapped[int | None] = mapped_column(index=True)
    gcd_id: Mapped[int | None] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(255))


class Imprint(Base):
    __tablename__ = "imprint"

    id: Mapped[int] = mapped_column(primary_key=True)
    metron_id: Mapped[int | None] = mapped_column(unique=True)
    name: Mapped[str] = mapped_column(String(255))
    publisher_id: Mapped[int | None] = mapped_column(ForeignKey("publisher.id"))


series_tag = Table(
    "series_tag",
    Base.metadata,
    Column("series_id", ForeignKey("series.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True),
)

issue_tag = Table(
    "issue_tag",
    Base.metadata,
    Column("issue_id", ForeignKey("issue.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True),
)


class Tag(Base):
    """Free-form user tag (e.g. "Explicit"); written to ComicInfo <Tags>."""

    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)


class Series(Base):
    __tablename__ = "series"

    id: Mapped[int] = mapped_column(primary_key=True)
    metron_id: Mapped[int | None] = mapped_column(unique=True)
    cv_id: Mapped[int | None] = mapped_column(index=True)
    gcd_id: Mapped[int | None] = mapped_column(index=True)
    # 'metron' or 'cv': which service this row is refreshed from.
    source: Mapped[str] = mapped_column(String(10), default="metron")

    name: Mapped[str] = mapped_column(String(255))
    sort_name: Mapped[str | None] = mapped_column(String(255))
    volume: Mapped[int | None]
    year_began: Mapped[int | None]
    year_end: Mapped[int | None]
    # Metron series_type name: Single Issue, Trade Paperback, Annual, ...
    series_type: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str | None] = mapped_column(String(50))
    desc: Mapped[str | None] = mapped_column(Text)
    genres: Mapped[list | None] = mapped_column(JSON)
    publisher_id: Mapped[int | None] = mapped_column(ForeignKey("publisher.id"))
    imprint_id: Mapped[int | None] = mapped_column(ForeignKey("imprint.id"))
    modified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Ours
    monitored: Mapped[bool] = mapped_column(Boolean, default=False)
    # New issues found by a Metron refresh start monitored (Sonarr's "monitor new items").
    monitor_new: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    path: Mapped[str | None] = mapped_column(String(1024))
    # How releases number this series' issues: 'issues' (#12), 'volumes' (Vol. 12:
    # manga and digests, which ComicVine lists as issues), or 'auto' (volumes when
    # the issues are titled "Volume N", as ComicVine names manga volumes).
    numbering: Mapped[str] = mapped_column(String(10), default="auto", server_default="auto")
    # 'rtl' = manga read right to left (ComicInfo <Manga>YesAndRightToLeft</Manga>),
    # 'ltr' = explicitly not; None = unknown, nothing written.
    reading_direction: Mapped[str | None] = mapped_column(String(3))
    # Metron series the owner said are not this one (a wrong ComicVine id on Metron,
    # or numbering that doesn't fit): never matched or suggested again.
    rejected_metron_ids: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    # When a ComicVine-sourced series was last checked for a Metron equivalent.
    metron_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Metron age-rating vocabulary; overrides the rating calculated from issues.
    age_rating_override: Mapped[str | None] = mapped_column(String(30))
    # Field names a user edited; a refresh never overwrites these.
    locked_fields: Mapped[list] = mapped_column(JSON, default=list)
    last_synced: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    added: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Metron matching: pending, matched, review, not_found, accepted (suggestion accepted,
    # waiting for the import job to apply it)
    match_status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", index=True
    )
    match_note: Mapped[str | None] = mapped_column(String(500))
    # Mylar's own book type (Print, TPB, GN, HC, One-Shot, Digital), kept as a matching hint.
    mylar_type: Mapped[str | None] = mapped_column(String(20))
    # A Metron series found by name and year when the ComicVine id found nothing;
    # it is only used once the user accepts it.
    suggested_metron_id: Mapped[int | None]
    # Mylar's own name and start year, kept so a Metron match can be compared
    # with what the user had (Metron's values overwrite name/year_began).
    mylar_name: Mapped[str | None] = mapped_column(String(255))
    mylar_year: Mapped[int | None]
    # Who confirmed the Metron match: 'auto' (ComicVine id + same name and year)
    # or 'user'. Fumora never writes files of a series whose match is unconfirmed.
    confirmed_by: Mapped[str | None] = mapped_column(String(10))
    # When our issues were last paired with Metron's issue list.
    issues_synced: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    publisher: Mapped[Publisher | None] = relationship()
    imprint: Mapped[Imprint | None] = relationship()

    @property
    def issues_are_volumes(self) -> bool:
        if self.numbering in ("issues", "volumes"):
            return self.numbering == "volumes"
        titled = [i.title for i in self.issues if i.title]
        named = [t for t in titled if _VOLUME_TITLE.match(t)]
        return bool(named) and len(named) * 2 >= len(titled)

    @property
    def writable(self) -> bool:
        """Fumora may write this series' files: its Metron match (if any) is confirmed."""
        return not (self.match_status == "matched" and self.confirmed_by is None)

    issues: Mapped[list["Issue"]] = relationship(back_populates="series", cascade="all, delete-orphan")
    tags: Mapped[list[Tag]] = relationship(secondary=series_tag)


class Issue(Base):
    __tablename__ = "issue"
    __table_args__ = (UniqueConstraint("series_id", "number", name="uq_issue_series_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"), index=True)
    metron_id: Mapped[int | None] = mapped_column(unique=True)
    cv_id: Mapped[int | None] = mapped_column(index=True)
    gcd_id: Mapped[int | None] = mapped_column(index=True)

    number: Mapped[str] = mapped_column(String(20))
    alt_number: Mapped[str | None] = mapped_column(String(20))
    # Numeric sort key derived from `number` ("1.MU" -> 1.0, "½" -> 0.5).
    sort_number: Mapped[float | None]
    title: Mapped[str | None] = mapped_column(String(500))
    cover_date: Mapped[date | None] = mapped_column(Date)
    store_date: Mapped[date | None] = mapped_column(Date)
    foc_date: Mapped[date | None] = mapped_column(Date)
    # Metron age-rating vocabulary (Unknown, Everyone, Teen, Teen Plus, Mature, Explicit, Adult).
    rating: Mapped[str | None] = mapped_column(String(30))
    isbn: Mapped[str | None] = mapped_column(String(20))
    upc: Mapped[str | None] = mapped_column(String(30))
    page_count: Mapped[int | None]
    price: Mapped[float | None]
    modified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Mylar's old status (Wanted, Downloaded, Skipped, Ignored), for reference only.
    # Whether an issue is wanted is derived: see monitoring.py.
    status: Mapped[str] = mapped_column(String(20), default="Skipped")
    monitored: Mapped[bool] = mapped_column(Boolean, default=False)
    rating_override: Mapped[str | None] = mapped_column(String(30))
    locked_fields: Mapped[list] = mapped_column(JSON, default=list)
    last_synced: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Metron matching: matched, unmatched (ours only), new (Metron only, added by a sync)
    match_status: Mapped[str | None] = mapped_column(String(20))
    # Full Metron issue record (credits, characters, ...), cached for tagging.
    metron_detail: Mapped[dict | None] = mapped_column(JSON)
    detail_fetched: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Full ComicVine issue record, for issues of series Metron doesn't have.
    cv_detail: Mapped[dict | None] = mapped_column(JSON)

    series: Mapped[Series] = relationship(back_populates="issues")
    files: Mapped[list["File"]] = relationship(back_populates="issue")
    tags: Mapped[list[Tag]] = relationship(secondary=issue_tag)


class Reprint(Base):
    """A collected edition (TPB issue) and one issue it reprints."""

    __tablename__ = "reprint"

    collection_issue_id: Mapped[int] = mapped_column(
        ForeignKey("issue.id", ondelete="CASCADE"), primary_key=True
    )
    reprinted_issue_id: Mapped[int] = mapped_column(
        ForeignKey("issue.id", ondelete="CASCADE"), primary_key=True
    )


class File(Base):
    __tablename__ = "file"

    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issue.id", ondelete="SET NULL"), index=True)
    path: Mapped[str] = mapped_column(String(1024), unique=True)
    size: Mapped[int | None]
    crc: Mapped[str | None] = mapped_column(String(16))
    tagged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Last dry-run result: {"fields": {name: [old, new]}, "error": str | None}
    tag_preview: Mapped[dict | None] = mapped_column(JSON)
    added: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    issue: Mapped[Issue | None] = relationship(back_populates="files")


class Grab(Base):
    """A release sent to a download client, followed until it is imported."""

    __tablename__ = "grab"

    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issue.id", ondelete="SET NULL"), index=True)
    # Every issue the release should contain (a pack covers several).
    issue_ids: Mapped[list] = mapped_column(JSON, default=list)
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id", ondelete="SET NULL"), index=True)
    indexer: Mapped[str] = mapped_column(String(100))
    release_title: Mapped[str] = mapped_column(String(500))
    guid: Mapped[str | None] = mapped_column(String(1000), index=True)
    # Prowlarr's link carries its API key: never rendered or logged.
    download_url: Mapped[str | None] = mapped_column(Text)
    protocol: Mapped[str] = mapped_column(String(10), default="usenet")
    size: Mapped[int | None]
    score: Mapped[int | None]
    source: Mapped[str | None] = mapped_column(String(20))
    client: Mapped[str] = mapped_column(String(50))
    client_job_id: Mapped[str | None] = mapped_column(String(100))
    # queued, downloading, importing, imported, failed, import_failed, removed
    state: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    progress: Mapped[float] = mapped_column(default=0.0)
    message: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str | None] = mapped_column(String(1024))
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Blocklist(Base):
    """Releases never to grab again (failed downloads, bad imports, or by hand)."""

    __tablename__ = "blocklist"

    id: Mapped[int] = mapped_column(primary_key=True)
    guid: Mapped[str | None] = mapped_column(String(1000), index=True)
    title: Mapped[str] = mapped_column(String(500))
    reason: Mapped[str | None] = mapped_column(String(500))
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    """Activity log shown in the UI."""

    __tablename__ = "event"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(10), default="info")
    kind: Mapped[str] = mapped_column(String(50))
    message: Mapped[str] = mapped_column(Text)
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id", ondelete="SET NULL"))
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issue.id", ondelete="SET NULL"))


class Job(Base):
    """A background job (import, refresh, ...). Survives restarts."""

    __tablename__ = "job"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(50), index=True)
    # queued, running, done, failed, cancelled
    state: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    phase: Mapped[str | None] = mapped_column(String(50))
    progress: Mapped[int] = mapped_column(default=0)
    total: Mapped[int] = mapped_column(default=0)
    message: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Release(Base):
    """An issue on Metron's release schedule (this week and the next few)."""

    __tablename__ = "release"

    id: Mapped[int] = mapped_column(primary_key=True)
    metron_issue_id: Mapped[int] = mapped_column(unique=True)
    metron_series_id: Mapped[int] = mapped_column(index=True)
    series_name: Mapped[str] = mapped_column(String(255))
    series_year: Mapped[int | None]
    number: Mapped[str] = mapped_column(String(20))
    store_date: Mapped[date | None] = mapped_column(Date, index=True)
    cover_date: Mapped[date | None] = mapped_column(Date)
    image: Mapped[str | None] = mapped_column(String(500))
    fetched: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Setting(Base):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
