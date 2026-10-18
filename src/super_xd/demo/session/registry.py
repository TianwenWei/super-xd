"""Local Demo session ownership and idle resource reclamation."""
import asyncio
import time


class SessionRegistry(dict):
    async def remove(self, session_id):
        actor = self.pop(session_id, None)
        if actor is not None:
            await actor.close()
        return actor is not None

    async def reap(self, *, idle_seconds=1800, now=None):
        now = time.monotonic() if now is None else now
        expired = [identity for identity, actor in list(self.items())
            if not actor.clients and not actor.audio_clients and not actor.generating
            and now - actor.last_activity >= idle_seconds
            and not any(task['status'] in {'queued', 'running', 'cancelling', 'waiting_clarification'}
                        for task in actor.tasks.values())]
        for identity in expired:
            await self.remove(identity)
        return expired

    async def close(self):
        actors = list(self.values())
        self.clear()
        await asyncio.gather(*(actor.close() for actor in actors))
