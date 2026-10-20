"""Import files page (manual import, see manual_import.py)."""

import re

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..manual_import import MANUAL_IMPORT, ImportPathError, check_path, propose, resolve, roots
from ..models import Series

_ID = re.compile(r"\[(\d+)\]\s*$")


def series_label(s: Series) -> str:
    return f"{s.name}{f' ({s.year_began})' if s.year_began else ''} [{s.id}]"


def register_import_routes(app: FastAPI, templates: Jinja2Templates, get_db) -> None:
    def _page(request: Request, db: Session, path: str, rows, error: str | None = None, **extra):
        series = db.scalars(select(Series).order_by(Series.sort_name, Series.name)).all()
        labels = {s.id: series_label(s) for s in series}
        return templates.TemplateResponse(
            request,
            "manual_import.html",
            {
                "path": path,
                "rows": rows,
                "error": error,
                "labels": labels,
                "roots": roots(db),
                "running": app.state.jobs.active(MANUAL_IMPORT),
                **extra,
            },
        )

    @app.get("/manual-import", response_class=HTMLResponse)
    def manual_import_page(request: Request, path: str = "", db: Session = Depends(get_db)):
        rows, error = [], None
        if path:
            try:
                rows = propose(db, check_path(db, path))
                db.rollback()  # proposing may fill in a series folder; nothing is saved
            except ImportPathError as exc:
                error = str(exc)
        return _page(request, db, path, rows, error)

    @app.post("/manual-import", response_class=HTMLResponse)
    async def manual_import_start(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        path = str(form.get("path") or "")
        mode = "hardlink" if form.get("mode") == "hardlink" else "move"
        paths, series, numbers = form.getlist("file"), form.getlist("series"), form.getlist("number")
        chosen = {int(i) for i in form.getlist("include") if str(i).isdigit()}
        if not (len(paths) == len(series) == len(numbers)):
            raise HTTPException(400, "The form came back incomplete; reload the page.")
        rows, items, issues = [], [], set()
        for n, (p, label, number) in enumerate(zip(paths, series, numbers, strict=True)):
            try:
                real = check_path(db, str(p))  # only ever the checked path from here on
            except ImportPathError as exc:
                raise HTTPException(400, str(exc)) from exc
            m = _ID.search(str(label))
            row = resolve(db, real, int(m.group(1)) if m else None, str(number))
            if label and not m and not row.problem:
                row.problem = "Pick the series from the list."
            if row.ready and row.issue_id in issues and n in chosen:
                row.problem = "Another row is already importing as this issue."
            if n in chosen and row.ready:
                items.append({"path": row.path, "issue_id": row.issue_id})
                issues.add(row.issue_id)
            rows.append(row)
        db.rollback()
        problems = [r for n, r in enumerate(rows) if n in chosen and not r.ready]
        if problems or not items:
            error = (
                f"{len(problems)} ticked rows need fixing first." if problems else "Tick at least one file."
            )
            return _page(request, db, path, rows, error, ticked=chosen, mode=mode)
        if app.state.jobs.active(MANUAL_IMPORT) is not None:
            return _page(request, db, path, rows, "An import is already running; see Jobs.", ticked=chosen)
        app.state.jobs.start(MANUAL_IMPORT, {"items": items, "mode": mode, "path": path})
        return RedirectResponse("/jobs", status_code=303)
