"""Series pages and the Mylar import page."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from .. import annuals, monitoring, naming, settings_store
from ..import_job import KIND as MYLAR_IMPORT
from ..importer import (
    apply_metron_series,
    clear_metron_values,
    import_report,
    log,
    sync_series_issues,
    unmatch_series,
)
from ..library_jobs import ORGANIZE, RETAG, SCAN, naming_settings, shared_folders
from ..metron import MetronError
from ..models import File, Issue, Series, Tag
from ..ratings import METRON_RATINGS, series_rating, strictest
from ..recycle import recycle_dir
from ..series_delete import DeleteFailed, delete_series, plan_delete
from ..sources import CHECK_NOTE, CV_REFRESH, accept_suggestion, reject_metron_match
from .activity_routes import recent_history

MATCH_LABELS = {
    "matched": "Matched",
    "review": "Needs review",
    "not_found": "Not on Metron",
    "pending": "Not matched yet",
    "accepted": "Accepted, applying",
}

_BULK_MONITORED = {"on": True, "off": False}
_BULK_MONITOR_NEW = {"on": True, "off": False}
_BULK_NUMBERING = ("auto", "issues", "volumes")
_BULK_READING_DIR = {"unknown": None, "ltr": "ltr", "rtl": "rtl"}
_BULK_AGE_RATING = {"calculated": None, **{r: r for r in METRON_RATINGS}}


def parse_tag_names(raw: str) -> list[str]:
    wanted: list[str] = []
    for name in (t.strip() for t in raw.split(",")):
        if name and name.lower() not in {w.lower() for w in wanted}:
            wanted.append(name[:100])
    return wanted


def resolve_tags(db: Session, raw: str) -> list[Tag]:
    tags: list[Tag] = []
    for name in parse_tag_names(raw):
        tag = db.scalar(select(Tag).where(func.lower(Tag.name) == name.lower()))
        if tag is None:
            tag = Tag(name=name)
            db.add(tag)
            db.flush()
        tags.append(tag)
    return tags


def _series_rows(db: Session, where=None, name_like: str | None = None, having_unmatched: bool = False):
    total = func.count(func.distinct(Issue.id))
    have = func.count(func.distinct(File.issue_id))
    unmatched = func.count(func.distinct(case((Issue.match_status == "unmatched", Issue.id))))
    q = (
        select(Series, total, have, unmatched)
        .outerjoin(Issue, Issue.series_id == Series.id)
        .outerjoin(File, File.issue_id == Issue.id)
        .group_by(Series.id)
        .order_by(func.lower(func.coalesce(Series.sort_name, Series.name)), Series.year_began)
    )
    if where is not None:
        q = q.where(where)
    if name_like:
        q = q.where(Series.name.ilike(f"%{name_like}%"))
    if having_unmatched:
        q = q.having(unmatched > 0)
    return [
        {"series": s, "total": t, "have": h or 0, "unmatched": u or 0} for s, t, h, u in db.execute(q).all()
    ]


def register_library_routes(app: FastAPI, templates: Jinja2Templates, get_db) -> None:
    templates.env.globals["match_labels"] = MATCH_LABELS

    def _series_list_page(
        request: Request,
        db: Session,
        match: str | None = None,
        q: str | None = None,
        notice: str | None = None,
    ):
        where = Series.match_status == match if match in MATCH_LABELS else None
        rows = _series_rows(db, where, name_like=q)
        return templates.TemplateResponse(
            request,
            "series_list.html",
            {
                "rows": rows,
                "match": match,
                "q": q or "",
                "notice": notice,
                "ratings": METRON_RATINGS,
                "monitor_options": monitoring.OPTIONS,
            },
        )

    @app.get("/series", response_class=HTMLResponse)
    def series_list(
        request: Request, match: str | None = None, q: str | None = None, db: Session = Depends(get_db)
    ):
        return _series_list_page(request, db, match=match, q=q)

    @app.post("/series/bulk-edit", response_class=HTMLResponse)
    def series_bulk_edit(
        request: Request,
        series_ids: Annotated[list[str], Form()] = (),
        monitored: str = Form(""),
        monitor_new: str = Form(""),
        monitor_option: str = Form(""),
        numbering: str = Form(""),
        reading_direction: str = Form(""),
        age_rating: str = Form(""),
        add_tags: str = Form(""),
        remove_tags: str = Form(""),
        match: str | None = Form(None),
        q: str | None = Form(None),
        db: Session = Depends(get_db),
    ):
        ids: list[int] = []
        for raw_id in series_ids:
            try:
                sid = int(raw_id)
            except ValueError as exc:
                raise HTTPException(400, f"Invalid series_ids value: {raw_id!r}") from exc
            if sid not in ids:
                ids.append(sid)

        for field, value, allowed in (
            ("monitored", monitored, _BULK_MONITORED),
            ("monitor_new", monitor_new, _BULK_MONITOR_NEW),
            ("monitor_option", monitor_option, monitoring.OPTIONS),
            ("numbering", numbering, _BULK_NUMBERING),
            ("reading_direction", reading_direction, _BULK_READING_DIR),
            ("age_rating", age_rating, _BULK_AGE_RATING),
        ):
            if value != "" and value not in allowed:
                raise HTTPException(400, f"Invalid {field} value: {value!r}")

        add_names = parse_tag_names(add_tags)
        remove_names = parse_tag_names(remove_tags)
        remove_by_lower = {n.lower(): n for n in remove_names}
        overlap = [
            f"{n} ({remove_by_lower[n.lower()]})" if n != remove_by_lower[n.lower()] else n
            for n in add_names
            if n.lower() in remove_by_lower
        ]
        if overlap:
            raise HTTPException(400, f"Cannot both add and remove tag: {', '.join(overlap)}")

        changes: list[str] = []
        if monitor_option != "":
            changes.append(f"monitoring set to {monitoring.OPTIONS[monitor_option]}")
        if monitored != "":
            changes.append(f"monitored {'on' if _BULK_MONITORED[monitored] else 'off'}")
        if monitor_new != "":
            changes.append(f"monitor new issues {'on' if _BULK_MONITOR_NEW[monitor_new] else 'off'}")
        if numbering != "":
            changes.append(f"numbering {numbering}")
        if reading_direction != "":
            rd = _BULK_READING_DIR[reading_direction]
            changes.append(f"reading direction {rd or 'unknown'}")
        if age_rating != "":
            ar = _BULK_AGE_RATING[age_rating]
            changes.append(f"age rating {ar or 'calculated'}")
        if add_names:
            changes.append(f"tags added: {', '.join(add_names)}")
        if remove_names:
            changes.append(f"tags removed: {', '.join(remove_names)}")

        if not ids:
            return _series_list_page(
                request, db, match=match, q=q, notice="No series selected; nothing changed."
            )
        if not changes:
            return _series_list_page(
                request, db, match=match, q=q, notice="No changes selected; nothing changed."
            )

        found_by_id = {
            s.id: s
            for s in db.scalars(
                select(Series)
                .where(Series.id.in_(ids))
                .options(
                    selectinload(Series.issues).selectinload(Issue.files),
                    selectinload(Series.tags),
                )
            ).all()
        }
        found = [found_by_id[sid] for sid in ids if sid in found_by_id]
        missing_ids = [sid for sid in ids if sid not in found_by_id]
        changed = 0

        if found:
            tags_to_add = resolve_tags(db, add_tags) if add_names else []
            remove_lower = set(remove_by_lower)

            for s in found:
                s_changes: list[str] = []
                non_monitor_changed = False
                old_monitored = s.monitored
                old_issues = {i.id: i.monitored for i in s.issues}

                if monitor_option != "":
                    monitoring.apply_option(s, monitor_option)
                if monitored != "":
                    s.monitored = _BULK_MONITORED[monitored]

                if monitor_option != "" and (
                    (monitored == "" and s.monitored != old_monitored)
                    or any(i.monitored != old_issues[i.id] for i in s.issues)
                ):
                    s_changes.append(f"monitoring set to {monitoring.OPTIONS[monitor_option]}")
                if monitored != "" and s.monitored != old_monitored:
                    s_changes.append(f"monitored {'on' if s.monitored else 'off'}")

                if monitor_new != "":
                    val = _BULK_MONITOR_NEW[monitor_new]
                    if s.monitor_new != val:
                        s.monitor_new = val
                        s_changes.append(f"monitor new issues {'on' if val else 'off'}")
                if numbering != "" and s.numbering != numbering:
                    s.numbering = numbering
                    s_changes.append(f"numbering {numbering}")
                    non_monitor_changed = True
                if reading_direction != "":
                    rd = _BULK_READING_DIR[reading_direction]
                    if s.reading_direction != rd:
                        s.reading_direction = rd
                        s_changes.append(f"reading direction {rd or 'unknown'}")
                        non_monitor_changed = True
                if age_rating != "":
                    ar = _BULK_AGE_RATING[age_rating]
                    if s.age_rating_override != ar:
                        s.age_rating_override = ar
                        s_changes.append(f"age rating {ar or 'calculated'}")
                        non_monitor_changed = True
                if tags_to_add:
                    existing = {t.name.lower() for t in s.tags}
                    added: list[str] = []
                    for tag in tags_to_add:
                        if tag.name.lower() not in existing:
                            s.tags.append(tag)
                            existing.add(tag.name.lower())
                            added.append(tag.name)
                    if added:
                        s_changes.append(f"tags added: {', '.join(added)}")
                        non_monitor_changed = True
                if remove_lower:
                    by_lower = {t.name.lower(): t.name for t in s.tags}
                    removed = [by_lower[n.lower()] for n in remove_names if n.lower() in by_lower]
                    if removed:
                        s.tags = [t for t in s.tags if t.name.lower() not in remove_lower]
                        s_changes.append(f"tags removed: {', '.join(removed)}")
                        non_monitor_changed = True

                if s_changes:
                    changed += 1
                    log(
                        db,
                        f"'{s.name}': {', '.join(s_changes)}",
                        kind="series" if non_monitor_changed else "monitor",
                        series_id=s.id,
                    )

            db.commit()

        unchanged = len(found) - changed
        notice = f"Changed {changed} series: {', '.join(changes)}."
        if unchanged:
            notice += f" {unchanged} already had these settings."
        if missing_ids:
            notice += f" Not found: {', '.join(str(i) for i in missing_ids)}."
        return _series_list_page(request, db, match=match, q=q, notice=notice)

    def detail_context(db: Session, series: Series, error: str | None = None, rename: bool = False) -> dict:
        issues = sorted(series.issues, key=lambda i: (i.sort_number is None, i.sort_number or 0, i.number))
        jobs = {kind: app.state.jobs.latest(kind) for kind in (SCAN, RETAG, ORGANIZE, "refresh")}

        def busy(kind: str) -> bool:
            j = jobs[kind]
            if j is None or j.state not in ("queued", "running"):
                return False
            ids = (j.params or {}).get("series_ids") or []
            # A refresh only blocks the page when it is refreshing this series.
            return kind != "refresh" or series.id in ids

        running = any(busy(k) for k in jobs)
        plan = None
        if rename:
            plan = naming.plan_series(
                series, shared_folder=series.path in shared_folders(db), **naming_settings(db)
            )
        return {
            "s": series,
            "issues": issues,
            "error": error,
            "jobs": jobs,
            "running": running,
            "ratings": METRON_RATINGS,
            "calculated_rating": strictest(i.rating for i in series.issues),
            "effective_rating": series_rating(series),
            "tag_names": ", ".join(sorted(t.name for t in series.tags)),
            "plan": plan,
            "writable": series.writable,
            "history": recent_history(db, 25, series_id=series.id),
            "children": sorted(series.children, key=lambda c: (c.year_began or 0, c.name)),
            "parent_options": annuals.parent_candidates(db, series)[:30] if not series.children else [],
        }

    @app.get("/series/{series_id}", response_class=HTMLResponse)
    def series_detail(request: Request, series_id: int, rename: bool = False, db: Session = Depends(get_db)):
        series = db.scalar(
            select(Series)
            .where(Series.id == series_id)
            .options(selectinload(Series.issues).selectinload(Issue.files), selectinload(Series.publisher))
        )
        if series is None:
            raise HTTPException(404)
        return templates.TemplateResponse(
            request, "series_detail.html", detail_context(db, series, rename=rename)
        )

    def _get_series(db: Session, series_id: int) -> Series:
        series = db.get(Series, series_id)
        if series is None:
            raise HTTPException(404)
        return series

    @app.post("/series/{series_id}/confirm")
    def series_confirm(series_id: int, db: Session = Depends(get_db)):
        series = _get_series(db, series_id)
        if series.match_status == "matched":
            series.confirmed_by = "user"
            series.match_note = None
            log(db, f"Match confirmed for '{series.name}'", series_id=series.id)
            db.commit()
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.post("/series/{series_id}/not-on-metron")
    def series_not_on_metron(series_id: int, db: Session = Depends(get_db)):
        series = _get_series(db, series_id)
        reject_metron_match(series)  # never matched or suggested again
        unmatch_series(db, series, "Marked by hand as not on Metron")
        log(db, f"'{series.name}' marked as not on Metron", series_id=series.id)
        db.commit()
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.post("/series/{series_id}/edit")
    def series_edit(
        series_id: int,
        age_rating: str = Form(""),
        tags: str = Form(""),
        numbering: str = Form("auto"),
        reading_direction: str = Form(""),
        db: Session = Depends(get_db),
    ):
        series = _get_series(db, series_id)
        series.reading_direction = reading_direction if reading_direction in ("ltr", "rtl") else None
        series.numbering = numbering if numbering in ("auto", "issues", "volumes") else "auto"
        series.age_rating_override = age_rating if age_rating in METRON_RATINGS else None
        series.tags = resolve_tags(db, tags)
        db.commit()
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.post("/series/{series_id}/parent")
    def series_parent(series_id: int, parent_id: str = Form(""), db: Session = Depends(get_db)):
        series = _get_series(db, series_id)
        parent = _get_series(db, int(parent_id)) if parent_id.strip().isdigit() else None
        try:
            annuals.link(series, parent)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        log(
            db,
            f"'{series.name}' now belongs to '{parent.name}'"
            if parent
            else f"'{series.name}' belongs to no series",
            kind="series",
            series_id=series.id,
        )
        db.commit()
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.get("/series/{series_id}/delete", response_class=HTMLResponse)
    def series_delete_page(request: Request, series_id: int, db: Session = Depends(get_db)):
        series = _get_series(db, series_id)
        return templates.TemplateResponse(
            request,
            "series_delete.html",
            {"s": series, "plan": plan_delete(db, series), "recycle": recycle_dir(db), "error": None},
        )

    @app.post("/series/{series_id}/delete", response_class=HTMLResponse)
    def series_delete(
        request: Request, series_id: int, delete_files: bool = Form(False), db: Session = Depends(get_db)
    ):
        series = _get_series(db, series_id)
        recycle = recycle_dir(db)
        try:
            delete_series(db, series, delete_files, recycle)
        except DeleteFailed as exc:
            db.rollback()
            series = _get_series(db, series_id)
            return templates.TemplateResponse(
                request,
                "series_delete.html",
                {"s": series, "plan": plan_delete(db, series), "recycle": recycle, "error": str(exc)},
                status_code=409,
            )
        return RedirectResponse("/series", status_code=303)

    def _start(kind: str, params: dict) -> None:
        active = app.state.jobs.active(kind)
        if active is not None:
            raise HTTPException(409, f"A {kind} job is already running; try again when it finishes.")
        app.state.jobs.start(kind, params)

    @app.post("/series/{series_id}/refresh-cv")
    async def series_refresh_cv(series_id: int, db: Session = Depends(get_db)):
        series = _get_series(db, series_id)
        if series.source != "cv" or series.cv_id is None:
            raise HTTPException(400, "This series doesn't follow ComicVine.")
        if not settings_store.is_set(db, "comicvine.api_key"):
            raise HTTPException(400, "Save a ComicVine API key in Settings first.")
        _start(CV_REFRESH, {"series_ids": [series_id]})
        return RedirectResponse("/jobs", status_code=303)

    @app.post("/series/{series_id}/scan")
    async def series_scan(series_id: int, db: Session = Depends(get_db)):
        _get_series(db, series_id)
        _start(SCAN, {"series_ids": [series_id]})
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.post("/series/{series_id}/retag")
    async def series_retag(series_id: int, write: bool = Form(False), db: Session = Depends(get_db)):
        series = _get_series(db, series_id)
        if write and not series.writable:
            raise HTTPException(400, "Confirm the Metron match before writing tags.")
        _start(RETAG, {"series_ids": [series_id], "dry_run": not write})
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.post("/series/{series_id}/organize")
    async def series_organize(series_id: int, db: Session = Depends(get_db)):
        if not _get_series(db, series_id).writable:
            raise HTTPException(400, "Confirm the Metron match before renaming.")
        _start(ORGANIZE, {"series_ids": [series_id]})
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.post("/library/scan")
    async def library_scan():
        _start(SCAN, {})
        return RedirectResponse("/import", status_code=303)

    @app.post("/import/confirm-all")
    def import_confirm_all(db: Session = Depends(get_db)):
        n = 0
        for series in db.scalars(
            select(Series).where(Series.match_status == "matched", Series.confirmed_by.is_(None))
        ):
            series.confirmed_by = "user"
            series.match_note = None
            n += 1
        log(db, f"Confirmed {n} matches in bulk")
        db.commit()
        return RedirectResponse("/import", status_code=303)

    @app.post("/series/{series_id}/metron", response_class=HTMLResponse)
    async def series_set_metron(
        request: Request, series_id: int, metron_id: int = Form(...), db: Session = Depends(get_db)
    ):
        """Manually match a series to a Metron series id, then pair its issues."""
        series = db.get(Series, series_id)
        if series is None:
            raise HTTPException(404)
        user, password, token = settings_store.metron_login(db)
        error = None
        taken = db.scalar(select(Series).where(Series.metron_id == metron_id, Series.id != series.id))
        if taken is not None:
            error = f"Metron series {metron_id} is already matched to '{taken.name}'."
        elif not settings_store.metron_usable(user, password, token):
            error = "Save a Metron API token (or username and password) in Settings first."
        else:
            try:
                async with app.state.make_metron_client(user, password, token) as client:
                    detail = await client.series(metron_id)
                    apply_metron_series(db, series, detail)
                    series.match_status, series.match_note = "matched", "Matched by hand"
                    series.suggested_metron_id = None
                    series.confirmed_by = "user"
                    # Undo the previous match: drop issues it added (if they have no
                    # files) and forget the Metron ids it assigned.
                    for issue in list(series.issues):
                        if issue.match_status == "new" and not issue.files:
                            series.issues.remove(issue)
                            db.delete(issue)
                        else:
                            clear_metron_values(issue)
                    db.flush()
                    await sync_series_issues(db, client, series)
                    series.issues_synced = datetime.now(UTC)
                log(db, f"'{series.name}' matched by hand to Metron series {metron_id}", series_id=series.id)
                db.commit()
            except MetronError as exc:
                db.rollback()
                error = str(exc)
        if error:
            series = db.get(Series, series_id)
            return templates.TemplateResponse(
                request, "series_detail.html", detail_context(db, series, error=error), status_code=400
            )
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    def status_context(db: Session) -> dict:
        """What the polled status box needs: the job and the report counts."""
        missing_files = db.scalar(
            select(func.count())
            .select_from(Issue)
            .outerjoin(File, File.issue_id == Issue.id)
            .where(Issue.status == "Downloaded", File.id.is_(None))
        )
        return {
            "job": app.state.jobs.latest(MYLAR_IMPORT),
            "report": import_report(db),
            "missing_files": missing_files,
        }

    def import_context(db: Session) -> dict:
        return {
            **status_context(db),
            "review": _series_rows(db, Series.match_status.in_(("review", "not_found", "accepted"))),
            "with_unmatched": _series_rows(db, Series.match_status == "matched", having_unmatched=True),
            "unconfirmed": db.scalars(
                select(Series)
                .where(Series.match_status == "matched", Series.confirmed_by.is_(None))
                .order_by(Series.name)
            ).all(),
            "scan_job": app.state.jobs.latest(SCAN),
            "mylar_path": settings_store.get(db, "mylar.db_path"),
        }

    # Endpoints that start jobs are async: the job runner needs the event loop.
    @app.post("/series/{series_id}/accept-suggestion")
    async def series_accept_suggestion(series_id: int, db: Session = Depends(get_db)):
        series = db.get(Series, series_id)
        if series is None or not series.suggested_metron_id:
            raise HTTPException(404)
        if (series.match_note or "").startswith(CHECK_NOTE):
            # Suggested by the monthly Metron check: move it now, no Mylar involved.
            user, password, token = settings_store.metron_login(db)
            if not settings_store.metron_usable(user, password, token):
                raise HTTPException(
                    400, "Save a Metron API token (or username and password) in Settings first."
                )
            try:
                async with app.state.make_metron_client(user, password, token) as client:
                    await accept_suggestion(db, client, series)
                db.commit()
            except (MetronError, ValueError) as exc:
                db.rollback()
                raise HTTPException(502, str(exc)) from exc
            return RedirectResponse(f"/series/{series_id}", status_code=303)
        series.match_status = "accepted"
        db.commit()
        if app.state.jobs.active(MYLAR_IMPORT) is None:  # a running import applies it itself
            app.state.jobs.start(MYLAR_IMPORT, {"skip_read": True})
        return RedirectResponse(f"/series/{series_id}", status_code=303)

    @app.post("/import/accept-suggestions")
    async def import_accept_suggestions(db: Session = Depends(get_db)):
        for series in db.scalars(
            select(Series).where(Series.match_status == "review", Series.suggested_metron_id.is_not(None))
        ):
            series.match_status = "accepted"
        db.commit()
        if app.state.jobs.active(MYLAR_IMPORT) is None:  # a running import applies it itself
            app.state.jobs.start(MYLAR_IMPORT, {"skip_read": True})
        return RedirectResponse("/import", status_code=303)

    @app.get("/import", response_class=HTMLResponse)
    def import_page(request: Request, db: Session = Depends(get_db)):
        return templates.TemplateResponse(request, "import.html", import_context(db))

    @app.post("/import/start", response_class=HTMLResponse)
    async def import_start(request: Request, db: Session = Depends(get_db)):
        if app.state.jobs.active(MYLAR_IMPORT) is None:  # running already: just show its progress
            app.state.jobs.start(MYLAR_IMPORT)
        return templates.TemplateResponse(request, "_import_status.html", status_context(db))

    @app.get("/import/status", response_class=HTMLResponse)
    def import_status(request: Request, db: Session = Depends(get_db)):
        return templates.TemplateResponse(request, "_import_status.html", status_context(db))
