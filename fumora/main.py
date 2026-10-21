import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, monitoring, settings_store
from .auth import hash_password, load_or_create_secret, verify_password
from .auto_search import SEARCH, make_search_handler
from .comicvine import ComicVineClient, ComicVineError
from .comicvine import make_limiter as make_cv_limiter
from .config import Config, load_config
from .db import make_engine, make_session_factory, upgrade_db
from .ddl import DirectClient
from .details import DetailFetcher
from .downloaders import DownloaderError
from .getcomics import GetComicsClient, GetComicsError
from .import_job import KIND as MYLAR_IMPORT
from .import_job import make_handler as make_import_handler
from .jobs import JobRunner
from .library_jobs import ORGANIZE, RETAG, SCAN, make_organize_handler, make_retag_handler, make_scan_handler
from .manual_import import MANUAL_IMPORT, make_manual_import_handler
from .metron import MetronAuthError, MetronClient, MetronError, RateLimiter
from .models import Event, Issue, Series, User
from .perms import folder_problems
from .refresh import REFRESH, make_refresh_handler
from .scheduler import ensure_refresh_time, run_download_monitor, run_scheduler
from .shelfmark import ShelfmarkClient, ShelfmarkError
from .sources import CV_REFRESH, make_cv_refresh_handler
from .web.catalog_routes import register_catalog_routes
from .web.import_routes import register_import_routes
from .web.library import register_library_routes
from .web.library_admin import register_admin_routes
from .web.monitor_routes import register_monitor_routes
from .web.search_routes import register_search_routes

WEB_DIR = Path(__file__).resolve().parent / "web"
log = logging.getLogger("fumora")
PUBLIC_PATHS = ("/static/", "/login", "/setup", "/api/health")


def create_app(config: Config | None = None) -> FastAPI:
    config = config or load_config()
    config.config_dir.mkdir(parents=True, exist_ok=True)
    upgrade_db(config.db_url)
    engine = make_engine(config.db_url)
    SessionLocal = make_session_factory(engine)

    # One limiter for the whole process: all Metron calls share its budget.
    metron_limiter = RateLimiter()

    def apply_rate_setting() -> None:
        with SessionLocal() as db:
            try:
                metron_limiter.per_minute = max(
                    1, min(20, int(settings_store.get(db, "metron.requests_per_minute")))
                )
            except (TypeError, ValueError):
                metron_limiter.per_minute = 20

    apply_rate_setting()

    def make_metron_client(
        username: str | None, password: str | None, token: str | None = None
    ) -> MetronClient:
        return MetronClient(username or "", password or "", metron_limiter, token=token)

    jobs = JobRunner(SessionLocal)
    jobs.register(MYLAR_IMPORT, make_import_handler(config.config_dir, make_metron_client))
    jobs.register(SCAN, make_scan_handler())
    jobs.register(ORGANIZE, make_organize_handler())
    jobs.register(REFRESH, make_refresh_handler(make_metron_client))
    jobs.register(SEARCH, make_search_handler())

    # ComicVine (series Metron doesn't have): its own limiter, same rules.
    comicvine_limiter = make_cv_limiter()

    def make_cv_client(api_key: str) -> ComicVineClient:
        return ComicVineClient(api_key, comicvine_limiter)

    jobs.register(CV_REFRESH, make_cv_refresh_handler(make_cv_client))
    jobs.register(RETAG, make_retag_handler(make_metron_client, make_cv_client))
    # Full issue records just before an import, so new files get full tags (#24).
    details = DetailFetcher(make_metron_client, metron_limiter, make_cv_client, comicvine_limiter)
    jobs.register(MANUAL_IMPORT, make_manual_import_handler(details.fetch))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        with SessionLocal() as db:
            ensure_refresh_time(db)
            ensure_refresh_time(db, key="schedule.cv_refresh_time")
            for problem in folder_problems(db, config.config_dir):
                log.warning(problem)
        jobs.resume_all()
        tasks = []
        if config.scheduler:
            tasks.append(asyncio.create_task(run_scheduler(SessionLocal, jobs)))
        if config.download_monitor:
            tasks.append(
                asyncio.create_task(run_download_monitor(SessionLocal, jobs, before_import=details.fetch))
            )
        yield
        for t in tasks:
            t.cancel()
        await jobs.shutdown()
        engine.dispose()

    app = FastAPI(title="Fumora", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.session_factory = SessionLocal
    app.state.metron_limiter = metron_limiter
    app.state.make_metron_client = make_metron_client
    app.state.comicvine_limiter = comicvine_limiter
    app.state.make_cv_client = make_cv_client
    app.state.jobs = jobs

    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    templates.env.globals["version"] = __version__
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    def get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if path.startswith(PUBLIC_PATHS):
            return await call_next(request)
        with SessionLocal() as db:
            has_user = db.scalar(select(func.count()).select_from(User)) > 0
        if not has_user:
            return RedirectResponse("/setup", status_code=303)
        if not request.session.get("user_id"):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    # Added after the login middleware so it wraps it and the session is
    # available inside require_login.
    app.add_middleware(
        SessionMiddleware,
        secret_key=load_or_create_secret(config.secret_key_path),
        session_cookie="fumora_session",
        same_site="lax",
        max_age=60 * 60 * 24 * 30,
    )

    # --- first run, login --------------------------------------------------

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request, db: Session = Depends(get_db)):
        if db.scalar(select(func.count()).select_from(User)):
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request, "setup.html", {"error": None})

    @app.post("/setup", response_class=HTMLResponse)
    def setup_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        password2: str = Form(...),
        db: Session = Depends(get_db),
    ):
        if db.scalar(select(func.count()).select_from(User)):
            return RedirectResponse("/login", status_code=303)
        error = None
        if len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != password2:
            error = "Passwords do not match."
        if error:
            return templates.TemplateResponse(request, "setup.html", {"error": error}, status_code=400)
        user = User(username=username.strip(), password_hash=hash_password(password))
        db.add(user)
        db.add(Event(kind="auth", message=f"Admin account '{user.username}' created"))
        db.commit()
        request.session["user_id"] = user.id
        return RedirectResponse("/settings", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login", response_class=HTMLResponse)
    def login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        db: Session = Depends(get_db),
    ):
        user = db.scalar(select(User).where(User.username == username.strip()))
        if user is None or not verify_password(password, user.password_hash):
            return templates.TemplateResponse(
                request, "login.html", {"error": "Wrong username or password."}, status_code=401
            )
        request.session["user_id"] = user.id
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # --- pages ---------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, db: Session = Depends(get_db)):
        stats = {
            "series": db.scalar(select(func.count()).select_from(Series)),
            "monitored": db.scalar(
                select(func.count()).select_from(Series).where(Series.monitored.is_(True))
            ),
            "issues": db.scalar(select(func.count()).select_from(Issue)),
            "wanted": monitoring.counts(db)["wanted"],
        }
        events = db.scalars(select(Event).order_by(Event.ts.desc()).limit(20)).all()
        metron_ready = settings_store.metron_ready(db)
        problems = folder_problems(db, config.config_dir)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {"stats": stats, "events": events, "metron_ready": metron_ready, "problems": problems},
        )

    def settings_context(db: Session, saved: list[str] | None = None) -> dict:
        groups: dict[str, list[dict]] = {}
        for sdef in settings_store.SETTINGS:
            groups.setdefault(sdef.group, []).append(
                {
                    "def": sdef,
                    # Secrets are never sent to the browser, only whether one is saved.
                    "value": None if sdef.secret else (settings_store.get(db, sdef.key) or ""),
                    "is_set": settings_store.is_set(db, sdef.key),
                }
            )
        return {"groups": groups, "saved": saved}

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, db: Session = Depends(get_db)):
        return templates.TemplateResponse(request, "settings.html", settings_context(db))

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_submit(request: Request, db: Session = Depends(get_db)):
        form = {k: str(v) for k, v in (await request.form()).items()}
        changed = settings_store.apply_form(db, form)
        if "metron.requests_per_minute" in changed:
            db.commit()
            apply_rate_setting()
        if changed:
            labels = ", ".join(
                f"{settings_store.SETTINGS_BY_KEY[k].group} {settings_store.SETTINGS_BY_KEY[k].label}"
                for k in changed
            )
            db.add(Event(kind="settings", message=f"Settings changed: {labels}"))
        db.commit()
        return templates.TemplateResponse(request, "settings.html", settings_context(db, changed))

    @app.post("/settings/test/metron", response_class=HTMLResponse)
    async def test_metron(request: Request, db: Session = Depends(get_db)):
        username, password, token = settings_store.metron_login(db)
        if not settings_store.metron_ready(db):
            ok, message = False, "Save a Metron API token (or username and password) first."
        else:
            try:
                async with app.state.make_metron_client(username, password, token) as client:
                    await client.check_login()
                limiter = app.state.metron_limiter
                ok = True
                message = "Connected with the API token." if token else "Connected (username and password)."
                if limiter.sustained_remaining is not None:
                    message += f" {limiter.sustained_remaining}"
                    if limiter.sustained_limit:
                        message += f" of {limiter.sustained_limit:,}"
                    message += " requests left today."
            except MetronAuthError:
                ok, message = (
                    False,
                    "Metron rejected the " + ("API token." if token else "username or password."),
                )
            except MetronError as exc:
                ok, message = False, str(exc)
            except Exception as exc:  # network errors
                ok, message = False, f"Could not reach Metron: {type(exc).__name__}"
        return templates.TemplateResponse(request, "_test_result.html", {"ok": ok, "message": message})

    @app.post("/settings/test/comicvine", response_class=HTMLResponse)
    async def test_comicvine(request: Request, db: Session = Depends(get_db)):
        key = settings_store.get(db, "comicvine.api_key")
        if not key:
            ok, message = False, "Save a ComicVine API key first."
        else:
            try:
                async with make_cv_client(key) as cv:
                    await cv.check()
                ok, message = True, "Connected."
            except ComicVineError as exc:
                ok, message = False, str(exc)
        return templates.TemplateResponse(request, "_test_result.html", {"ok": ok, "message": message})

    @app.post("/settings/test/getcomics", response_class=HTMLResponse)
    async def test_getcomics(request: Request, db: Session = Depends(get_db)):
        get = lambda k: settings_store.get(db, k)  # noqa: E731
        try:
            async with GetComicsClient(get("getcomics.url"), get("flaresolverr.url")) as gc:
                posts = await gc.check()
            async with DirectClient(get("ddl.path")) as ddl:
                folder = await ddl.check()
            ok, message = True, f"GetComics answered ({posts} posts on its front page); {folder} is writable."
        except (GetComicsError, DownloaderError) as exc:
            ok, message = False, str(exc)
        return templates.TemplateResponse(request, "_test_result.html", {"ok": ok, "message": message})

    @app.post("/settings/test/shelfmark", response_class=HTMLResponse)
    async def test_shelfmark(request: Request, db: Session = Depends(get_db)):
        url = settings_store.get(db, "shelfmark.url")
        if not url:
            ok, message = False, "Save Shelfmark's URL first."
        else:
            try:
                async with ShelfmarkClient(url, settings_store.get(db, "shelfmark.api_key")) as sm:
                    await sm.check()
                ok, message = True, "Connected."
            except ShelfmarkError as exc:
                ok, message = False, str(exc)
        return templates.TemplateResponse(request, "_test_result.html", {"ok": ok, "message": message})

    register_library_routes(app, templates, get_db)
    register_admin_routes(app, templates, get_db)
    register_monitor_routes(app, templates, get_db)
    register_catalog_routes(app, templates, get_db)
    register_search_routes(app, templates, get_db)
    register_import_routes(app, templates, get_db)

    @app.get("/api/health")
    def health():
        return JSONResponse({"status": "ok", "version": __version__})

    return app
