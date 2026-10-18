"""Ports used by one Thinker execution; the caller owns storage and scheduling."""

from datetime import datetime, timezone
from typing import Any, Protocol
from typing_extensions import Unpack
from super_xd.nlp.contracts.task_state import TaskState, TaskPatch


class TaskStore(Protocol):
    """Authoritative versioned task state, independent of its persistence backend.

    Snapshots and fields are JSON-compatible values. Implementations must return
    detached snapshots, commit saves atomically against the supplied revision,
    reject writes to terminal revisions, and preserve accumulated budgets across
    continuation. No method claims work or starts another run.
    """

    async def read(self, task_id: str) -> TaskState | None: ...

    async def refresh(self, task_id: str) -> TaskState | None:
        """Apply already accepted input updates and return the latest revision."""
        ...

    async def save(self, task_id: str, revision: int, **fields: Unpack[TaskPatch]) -> bool:
        """Compare-and-set state and checkpoint; False means it was superseded."""
        ...

    async def expire(self, task_id: str, max_age_seconds: float) -> None:
        """Close expired work using the caller's authoritative timeout policy."""
        ...

    async def notify_progress(self, task_id: str) -> bool: ...

    async def usage(self, task_id: str, session_id: str) -> tuple[float, int]:
        """Return cumulative quick-run seconds and lookup attempts, across resumes."""
        ...


class TaskTraceSink(Protocol):
    async def emit(self, session_id: str, kind: str, data: dict[str, Any],
                   **fields: Any) -> Any:
        """Record ordered diagnostics, redacting credentials before external storage.

        The caller must make quick execution/lookup events visible to its usage
        accounting before emit returns, so a fresh runner keeps the same budget.
        """
        ...


def running_elapsed(task: TaskState, now: datetime | None = None) -> float:
    seconds = float(task.get('running_seconds') or 0)
    if task.get('status') == 'running' and task.get('running_since'):
        since = datetime.fromisoformat(task['running_since'])
        seconds += max(0, ((now or datetime.now(timezone.utc)) - since).total_seconds())
    return seconds


class ResearchSearch(Protocol):
    async def search(self, query: str, max_results: int = 5, **options: Any) -> dict[str, Any]: ...
    async def extract(self, urls: list[str]) -> dict[str, Any]: ...


class ResearchForecast(Protocol):
    async def forecast(self, city: str, date: str, *, country_code: str | None = None) -> dict[str, Any]: ...
