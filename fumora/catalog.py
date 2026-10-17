"""Adding series from Metron (the way new series arrive once Mylar retires)."""

import os
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import monitoring, naming, settings_store
from .importer import apply_metron_series, log, sync_series_issues
from .metron import MetronClient
from .models import Series
from .scanner import scan_folders


def folder_for(db: Session, series: Series) -> str:
    """Where the series' folder goes: library root + the folder naming template."""
    root = (settings_store.get(db, "library.path") or "/data/media/comics").rstrip("/")
    template = settings_store.get(db, "naming.folder_template") or naming.DEFAULT_FOLDER
    colon = settings_store.get(db, "naming.colon") or "delete"
    apostrophes = settings_store.get(db, "naming.apostrophes") or "remove"
    return f"{root}/{naming.render_path(template, naming.context(series), colon, apostrophes)}"


async def add_series(
    db: Session, client: MetronClient, metron_id: int, option: str = "future", monitor_new: bool = True
) -> Series:
    """Create a series from Metron, list its issues, set monitoring, link files
    if its folder already exists. Creates no folders and touches no files."""
    existing = db.scalar(select(Series).where(Series.metron_id == metron_id))
    if existing is not None:
        return existing
    detail = await client.series(metron_id)
    series = Series(
        name=detail.get("name") or "",
        source="metron",
        match_status="matched",
        confirmed_by="user",
        monitored=option != "none",
        monitor_new=monitor_new,
        cv_id=detail.get("cv_id"),
        locked_fields=[],
    )
    db.add(series)
    apply_metron_series(db, series, detail)
    series.path = folder_for(db, series)
    db.flush()
    await sync_series_issues(db, client, series)
    if os.path.isdir(series.path):
        scan_folders(db, [series.id])
    db.flush()
    db.refresh(series)  # reload issues with the files the scan linked
    series.issues_synced = datetime.now(UTC)
    monitoring.apply_option(series, option)
    series.monitor_new = monitor_new
    log(
        db,
        f"Added '{series.name}' from Metron ({monitoring.OPTIONS[option]})",
        kind="add",
        series_id=series.id,
    )
    db.flush()
    return series
