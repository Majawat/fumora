"""In-process background jobs, persisted in the `job` table.

A job that was running when the app stopped is started again on the next
startup; job handlers are written to be idempotent so they just continue.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .models import Event, Job

log = logging.getLogger("fumora.jobs")


class JobBusy(Exception):
    """A job of this kind is already running: the new request was not started."""

    def __init__(self, kind: str, job_id: int):
        super().__init__(
            f"A {kind} job is already running (job {job_id}); try again when it finishes (Jobs)."
        )
        self.kind, self.job_id = kind, job_id


class JobContext:
    def __init__(self, job_id: int, session_factory: sessionmaker):
        self.job_id = job_id
        self.session_factory = session_factory
        self._threads: set[asyncio.Future] = set()

    async def thread(self, fn, *args):
        """Run blocking work (file moves, tagging) off the event loop. Unlike a bare
        asyncio.to_thread, a cancelled job waits for it to finish before it counts as
        stopped, so a new job can't start on the same files while it still runs."""
        fut = asyncio.ensure_future(asyncio.to_thread(fn, *args))
        self._threads.add(fut)
        fut.add_done_callback(self._threads.discard)
        return await asyncio.shield(fut)

    async def settle(self) -> None:
        if self._threads:
            await asyncio.gather(*self._threads, return_exceptions=True)

    def update(self, **fields) -> None:
        with self.session_factory() as db:
            job = db.get(Job, self.job_id)
            for key, value in fields.items():
                setattr(job, key, value)
            db.commit()

    def params(self) -> dict:
        with self.session_factory() as db:
            return dict(db.get(Job, self.job_id).params or {})


Handler = Callable[[JobContext], Awaitable[str | None]]


class JobRunner:
    def __init__(self, session_factory: sessionmaker):
        self.session_factory = session_factory
        self.handlers: dict[str, Handler] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._cancel_requested: set[int] = set()

    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    def active(self, kind: str) -> Job | None:
        with self.session_factory() as db:
            return db.scalar(
                select(Job)
                .where(Job.kind == kind, Job.state.in_(("queued", "running")))
                .order_by(Job.id.desc())
            )

    def latest(self, kind: str) -> Job | None:
        with self.session_factory() as db:
            return db.scalar(select(Job).where(Job.kind == kind).order_by(Job.id.desc()))

    def start(self, kind: str, params: dict | None = None) -> int:
        """Start a job. One job of a kind runs at a time: while one runs, JobBusy is
        raised (the request is never silently folded into the running job)."""
        existing = self.active(kind)
        if existing is not None:
            raise JobBusy(kind, existing.id)
        with self.session_factory() as db:
            job = Job(kind=kind, state="queued", params=params or {})
            db.add(job)
            db.commit()
            job_id = job.id
        self._spawn(job_id, kind)
        return job_id

    def resume_all(self) -> None:
        with self.session_factory() as db:
            jobs = db.scalars(
                select(Job).where(Job.state.in_(("queued", "running"))).order_by(Job.id.desc())
            ).all()
            latest: dict[str, int] = {}
            for job in jobs:  # one per kind, the newest; any older one is not resumed
                if job.kind in latest:
                    job.state, job.finished = "cancelled", datetime.now(UTC)
                    job.message = f"Not resumed: job {latest[job.kind]} of the same kind was newer"
                else:
                    latest[job.kind] = job.id
            db.commit()
        for kind, job_id in latest.items():
            self._spawn(job_id, kind)

    def cancel(self, job_id: int) -> bool:
        """Stop a running job. Its work so far is kept; it won't resume on restart."""
        task = self._tasks.get(job_id)
        with self.session_factory() as db:
            job = db.get(Job, job_id)
            if job is None or job.state not in ("queued", "running"):
                return False
            if task is None:  # queued/running in the DB but not in this process
                job.state, job.finished, job.message = "cancelled", datetime.now(UTC), "Cancelled"
                db.commit()
                return True
        self._cancel_requested.add(job_id)
        task.cancel()
        return True

    def recent(self, limit: int = 30) -> list[Job]:
        with self.session_factory() as db:
            return db.scalars(select(Job).order_by(Job.id.desc()).limit(limit)).all()

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    def _spawn(self, job_id: int, kind: str) -> None:
        self._tasks[job_id] = asyncio.create_task(self._run(job_id, kind))

    async def _run(self, job_id: int, kind: str) -> None:
        ctx = JobContext(job_id, self.session_factory)
        handler = self.handlers.get(kind)
        if handler is None:
            ctx.update(state="failed", message=f"No handler for job kind '{kind}'")
            return
        ctx.update(state="running", started=datetime.now(UTC))
        try:
            summary = await handler(ctx)
        except asyncio.CancelledError:
            await ctx.settle()  # blocking work already under way finishes first
            if job_id in self._cancel_requested:
                self._cancel_requested.discard(job_id)
                ctx.update(state="cancelled", finished=datetime.now(UTC), message="Cancelled")
                return
            # App shutting down: leave it 'running' so the next start resumes it.
            raise
        except Exception as exc:
            log.exception("job %s (%s) failed", job_id, kind)
            ctx.update(state="failed", finished=datetime.now(UTC), message=f"{type(exc).__name__}: {exc}")
            with self.session_factory() as db:
                db.add(Event(kind=kind, level="error", message=f"Job failed: {exc}"))
                db.commit()
        else:
            ctx.update(state="done", finished=datetime.now(UTC), message=summary)
        finally:
            self._tasks.pop(job_id, None)
