"""SQLite implementation of the Gateway task port, used only by the demo."""
from __future__ import annotations

import asyncio

from backend.worker import SQLiteTaskStore, SQLiteTaskTraceSink


class SQLiteGatewayTaskStore(SQLiteTaskStore, SQLiteTaskTraceSink):
    def __init__(self, db, conversation_id=None):
        self.db, self.conversation_id = db, conversation_id

    def for_scope(self, conversation_id, **lease):
        return type(self)(self.db, conversation_id)

    async def read(self, task_id):
        task = await super().read(task_id)
        return task if task and task['session_id'] == self.conversation_id else None

    async def refresh(self, task_id):
        if await self.read(task_id) is None:
            return None
        return await asyncio.to_thread(self.db.apply_pending, task_id)

    async def save(self, task_id, revision, **fields):
        if await self.read(task_id) is None:
            return False
        return await super().save(task_id, revision, **fields)

    async def create_task(self, task_id, goal, context, idempotency_key):
        return await asyncio.to_thread(self.db.create_task, self.conversation_id, goal, context,
                                       task_id=task_id, idempotency_key=idempotency_key)

    async def mutate_task(self, task_id, revision, action, text, idempotency_key):
        if await self.read(task_id) is None:
            raise ValueError('Task does not belong to this conversation')
        return await asyncio.to_thread(self.db.mutate_task, task_id, action, text, revision,
                                       idempotency_key=idempotency_key, requeue=True)
