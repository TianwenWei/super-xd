"""Async SQLite adapters shared by the local worker and Gateway ledger."""
import asyncio


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
