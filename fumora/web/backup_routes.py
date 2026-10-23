"""Backups page: list, back up now, download, delete, upload, restore (applied at the next start)."""

import os
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import backup
from ..importer import log
from ..perms import normalize

MAX_UPLOAD = 2 * 2**30


def register_backup_routes(app: FastAPI, templates: Jinja2Templates, get_db, config) -> None:
    folder = backup.backup_dir(config.config_dir)

    def page(request: Request, message: str | None = None, error: str | None = None, status: int = 200):
        return templates.TemplateResponse(
            request,
            "backups.html",
            {
                "backups": backup.list_backups(folder),
                "folder": folder,
                "pending": backup.restore_pending(config.config_dir),
                "message": message,
                "error": error,
            },
            status_code=status,
        )

    def _find(name: str) -> backup.BackupInfo:
        try:
            return backup.find(folder, name)
        except backup.BackupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/backups", response_class=HTMLResponse)
    def backups_page(request: Request):
        return page(request)

    @app.post("/backups/now", response_class=HTMLResponse)
    def backup_now(request: Request, db: Session = Depends(get_db)):
        try:
            made = backup.make_backup(config.db_path, folder, "manual")
        except OSError as exc:
            return page(request, error=f"Backup failed: {exc}", status=500)
        log(db, f"Backed up the database by hand: {made.name}", kind="backup")
        db.commit()
        return page(request, message=f"Saved {made.name}.")

    @app.get("/backups/{name}/download")
    def backup_download(name: str):
        b = _find(name)
        return FileResponse(b.path, filename=b.name, media_type="application/vnd.sqlite3")

    @app.post("/backups/{name}/delete")
    def backup_delete(name: str, db: Session = Depends(get_db)):
        b = _find(name)
        b.path.unlink(missing_ok=True)
        log(db, f"Deleted backup {b.name}", kind="backup")
        db.commit()
        return RedirectResponse("/backups", status_code=303)

    @app.post("/backups/{name}/restore", response_class=HTMLResponse)
    def backup_restore(request: Request, name: str, db: Session = Depends(get_db)):
        b = _find(name)
        try:
            backup.stage_restore(config.config_dir, b.path)
        except backup.BackupError as exc:
            return page(request, error=f"Can't restore {b.name}: {exc}", status=400)
        log(
            db,
            f"Restore of {b.name} staged: it takes effect when Fumora restarts",
            kind="backup",
            level="warning",
        )
        db.commit()
        return page(request, message=f"{b.name} will replace the database when Fumora next starts.")

    @app.post("/backups/cancel-restore")
    def backup_cancel(db: Session = Depends(get_db)):
        if backup.cancel_restore(config.config_dir):
            log(db, "Staged restore cancelled", kind="backup")
            db.commit()
        return RedirectResponse("/backups", status_code=303)

    @app.post("/backups/upload", response_class=HTMLResponse)
    async def backup_upload(
        request: Request, upload: Annotated[UploadFile, File()], db: Session = Depends(get_db)
    ):
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f".upload-{os.getpid()}-{id(upload)}.db"
        size = 0
        try:
            with open(tmp, "wb") as out:
                while chunk := await upload.read(2**20):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise backup.BackupError("That file is too big for a Fumora database.")
                    out.write(chunk)
            backup.check(tmp, backup.known_revisions())
            target = backup._new_path(folder, "uploaded")
            normalize(tmp)
            os.replace(tmp, target)
        except backup.BackupError as exc:
            return page(request, error=f"{upload.filename}: {exc}", status=400)
        finally:
            Path(tmp).unlink(missing_ok=True)
        log(db, f"Uploaded backup {upload.filename} as {target.name}", kind="backup")
        db.commit()
        return page(request, message=f"Uploaded as {target.name}. Restore it from the list.")
