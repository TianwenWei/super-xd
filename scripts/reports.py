"""Batch-scoped local reports; generated evidence never belongs in docs/."""

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4


def report_path(name: str) -> Path:
    """Allocate an independent batch directory without creating files yet."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return Path("data/evals") / f"{name}-{stamp}-{uuid4().hex[:8]}" / "report.json"


def write_report(report, destination: Path | str) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return destination
