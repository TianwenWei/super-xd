"""SQLite demo scheduling shell around the storage-independent TaskRunner."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from typing import Any

from .harness.task_runner import TaskRunner, _safe_error

logger = logging.getLogger(__name__)


class SQLiteTaskStore:
    """Adapt the demo task ledger without blocking an inference event loop."""

    def __init__(self, db):
        self.db = db

    async def read(self, task_id):
        return await asyncio.to_thread(self.db.get_task, task_id)

    async def refresh(self, task_id):
        return await asyncio.to_thread(self.db.apply_pending, task_id)

    async def save(self, task_id, revision, **fields):
        return await asyncio.to_thread(self.db.save_task, task_id, revision, **fields)

    async def expire(self, task_id, max_age_seconds):
        # The demo ledger owns its existing atomic timeout/status policy.
        await asyncio.to_thread(self.db.expire_tasks, max_age_seconds)

    async def notify_progress(self, task_id):
        return await asyncio.to_thread(self.db.notify_task_progress, task_id)

    async def usage(self, task_id, session_id):
        return await asyncio.to_thread(self._usage, task_id, session_id)

    def _usage(self, task_id, session_id):
        elapsed, lookups, cursor = 0.0, 0, 0
        while True:
            rows = self.db.traces_after(session_id, after_id=cursor, task_id=task_id)
            for row in rows:
                data = row["data"]
                if row["kind"] == "task.execution.finished" and data.get("query_mode") == "quick":
                    elapsed += max(0, float(data.get("elapsed_seconds", 0)))
                if row["kind"] == "tool.started" and data.get("name") == "lookup_web":
                    lookups += 1
            if len(rows) < 200:
                return elapsed, lookups
            cursor = rows[-1]["trace_id"]


class SQLiteTaskTraceSink:
    def __init__(self, db):
        self.db = db

    async def emit(self, session_id, kind, data, **fields):
        return await asyncio.to_thread(self.db.append_trace, session_id, kind, data, **fields)


class ThinkerWorker(TaskRunner):
    """Demo adapter for SQLite state, provider wiring and process scheduling."""

    def __init__(self, db: Any, ark: Any = None, tavily: Any = None, *,
                 max_steps: int = 20, timeout_seconds: float = 60,
                 concurrency: int = 2, close_clients: bool = True,
                 catalog: dict[str, Any] | None = None,
                 tool_timeout_seconds: float | None = None,
                 quick_max_steps: int = 4, quick_timeout_seconds: float = 60,
                 weather: Any = None):
        if ark is None or tavily is None:
            from .services.factory import create_llm, create_search
            ark = ark or create_llm()
            tavily = tavily or create_search()
        from .services.reads import verify_forecast_record
        self.db = db
        super().__init__(SQLiteTaskStore(db), ark, tavily,
                         trace_sink=SQLiteTaskTraceSink(db), max_steps=max_steps,
                         timeout_seconds=timeout_seconds, close_clients=close_clients,
                         catalog=catalog, tool_timeout_seconds=tool_timeout_seconds if tool_timeout_seconds is not None
                         else float(os.getenv("TOOL_TIMEOUT_SECONDS", "35")),
                         quick_max_steps=quick_max_steps, quick_timeout_seconds=quick_timeout_seconds,
                         weather=weather, forecast_verifier=verify_forecast_record)
        self.concurrency = max(1, concurrency)
        self._stopping = asyncio.Event()
        self._active: dict[str, asyncio.Task] = {}

    async def _execute_tool(self, name, *args, **kwargs):
        if name == "get_weather_forecast" and self.weather is None:
            from .services.factory import create_maps
            self.weather = create_maps()
        return await super()._execute_tool(name, *args, **kwargs)

    def stop(self) -> None:
        self._stopping.set()

    async def run_forever(self, poll_interval: float = 0.15) -> None:
        """Claim only free slots. Clarification returns immediately to this loop."""
        try:
            while not self._stopping.is_set():
                await asyncio.to_thread(self.db.expire_tasks, self.timeout_seconds)
                for task_id, future in list(self._active.items()):
                    current = await self.store.read(task_id)
                    if current and current['status'] in {'completed', 'failed', 'cancelled'} and not future.done():
                        future.cancel()
                    if future.done():
                        # run_task handles ordinary failures, but surface unexpected ones.
                        if not future.cancelled() and future.exception():
                            logger.error("Thinker task failed unexpectedly: %s", _safe_error(future.exception()))
                        del self._active[task_id]
                # A queued task can be cancelled before any worker claims it.
                # Database.apply_pending is also used by mutate_task in the actor.
                while len(self._active) < self.concurrency and not self._stopping.is_set():
                    task = await asyncio.to_thread(self.db.claim_next, exclude_ids=tuple(self._active))
                    if task is None:
                        break
                    if task["id"] in self._active:
                        # A cancelling task remains claimable until its currently
                        # active coroutine reaches a boundary. Yield to it.
                        break
                    self._active[task["id"]] = asyncio.create_task(self.run_task(task))
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=poll_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            for future in self._active.values():
                future.cancel()
            await asyncio.gather(*self._active.values(), return_exceptions=True)


async def _main() -> None:
    from .prompt_config import validate_all
    validate_all()
    from dotenv import load_dotenv
    load_dotenv()
    from .db import Database
    parser = argparse.ArgumentParser(description="Independent SQLite Thinker worker")
    parser.add_argument("--database", default=os.getenv("DATABASE_PATH", "data/super_xd.sqlite3"))
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("THINKER_CONCURRENCY", "2")))
    args = parser.parse_args()
    db = Database(args.database)
    from .services.factory import create_services
    services = create_services()
    worker = ThinkerWorker(db, ark=services.llm, tavily=services.web, weather=services.maps.weather,
                           close_clients=False, concurrency=args.concurrency,
                           max_steps=int(os.getenv("THINKER_MAX_STEPS", "20")),
                           timeout_seconds=float(os.getenv("THINKER_TIMEOUT_SECONDS", "60")))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)
    logger.info("Thinker started: concurrency=%s, max_steps=%s, timeout_seconds=%s",
                worker.concurrency, worker.max_steps, worker.timeout_seconds)
    try:
        await worker.run_forever()
    finally:
        await worker.aclose()
        await services.aclose()
        close = getattr(db, "close", None)
        if close:
            close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_main())
