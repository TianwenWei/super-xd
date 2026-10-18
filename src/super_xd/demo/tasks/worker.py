"""SQLite demo scheduling shell around the storage-independent TaskRunner."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from dataclasses import asdict
from typing import Any

if __name__ == "__main__":
    from super_xd.demo.environment import load_local_environment
    load_local_environment()

from super_xd.nlp.orchestration.tasks.runner import TaskRunner, _safe_error
from super_xd.nlp.release import get_release_id
from super_xd.demo.resources.runtime import freeze_local_model, local_model_policy_id, local_task_budget

from super_xd.demo.tasks.store import SQLiteTaskStore, SQLiteTaskTraceSink

logger = logging.getLogger(__name__)


class ThinkerWorker(TaskRunner):
    """Demo adapter for SQLite state, provider wiring and process scheduling."""

    def __init__(self, db: Any, ark: Any = None, tavily: Any = None, *,
                 max_steps: int | None = None, timeout_seconds: float | None = None,
                 concurrency: int = 2, close_clients: bool = True,
                 catalog: dict[str, Any] | None = None,
                 tool_timeout_seconds: float | None = None,
                 quick_max_steps: int | None = None, quick_timeout_seconds: float | None = None,
                 weather: Any = None):
        if ark is None or tavily is None:
            from super_xd.nlp.adapters.factory import create_llm, create_search
            ark = ark or create_llm()
            tavily = tavily or create_search()
        from super_xd.nlp.adapters.reads.services import verify_forecast_record
        self.db = db
        budget = local_task_budget(max_steps=max_steps, timeout_seconds=timeout_seconds,
            tool_timeout_seconds=tool_timeout_seconds, quick_max_steps=quick_max_steps,
            quick_timeout_seconds=quick_timeout_seconds)
        model = freeze_local_model(ark)
        self._model_owner = ark if model is not ark else None
        self.release_id, self.model_policy_id = get_release_id(), local_model_policy_id(model, budget)
        super().__init__(SQLiteTaskStore(db), model, tavily,
                         trace_sink=SQLiteTaskTraceSink(db), close_clients=close_clients,
                         catalog=catalog, **asdict(budget),
                         weather=weather, forecast_verifier=verify_forecast_record)
        self.concurrency = max(1, concurrency)
        self._stopping = asyncio.Event()
        self._active: dict[str, asyncio.Task] = {}

    async def run_task(self, task):
        current = await self.store.refresh(task['id'])
        if current is None:
            raise RuntimeError('Task no longer exists')
        if current['status'] in {'completed', 'failed', 'cancelled', 'waiting_clarification', 'awaiting_confirm'}:
            return
        context = current.get('context') or {}
        if current['status'] != 'cancelling' and (context.get('release_id') != self.release_id or
                context.get('model_policy_id') != self.model_policy_id):
            code = 'task_deployment_mismatch'
            await self._trace(current, 'task.deployment.rejected', {
                'code': code, 'registered_release_id': context.get('release_id'),
                'registered_model_policy_id': context.get('model_policy_id'),
                'release_id': self.release_id, 'model_policy_id': self.model_policy_id,
                'model_called': False}, status='failed')
            await self.store.save(current['id'], current['revision'], status='failed', error_code=code,
                error='任务登记版本与当前部署不一致，请重新创建任务。',
                summary='任务登记版本已过期或缺失，未开始执行。请重新创建任务。', question=None)
            return
        await super().run_task(current)

    async def _execute_tool(self, name, *args, **kwargs):
        if name == "get_weather_forecast" and self.weather is None:
            from super_xd.nlp.adapters.factory import create_maps
            self.weather = create_maps()
            self.tools.weather = self.weather
        return await super()._execute_tool(name, *args, **kwargs)

    def stop(self) -> None:
        self._stopping.set()

    async def aclose(self) -> None:
        try:
            await super().aclose()
        finally:
            # The frozen selector shares clients and deliberately does not own them.
            if self.close_clients and self._model_owner is not None:
                await self._model_owner.aclose()

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
    from super_xd.nlp.prompts.config import validate_all
    validate_all()
    from super_xd.demo.tasks.database import Database
    from super_xd.demo.settings import DATABASE_PATH
    parser = argparse.ArgumentParser(description="Independent SQLite Thinker worker")
    parser.add_argument("--database", default=DATABASE_PATH)
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("THINKER_CONCURRENCY", "2")))
    args = parser.parse_args()
    db = Database(args.database)
    from super_xd.demo.assembly import create_worker_services
    services = create_worker_services()
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
