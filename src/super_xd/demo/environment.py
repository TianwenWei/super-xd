"""Explicit .env bootstrap for local Demo/CLI entrypoints; production never calls it."""
from pathlib import Path
from super_xd.demo.paths import workspace_root


def load_local_environment(path: Path | None = None, *, override: bool = False) -> bool:
    from dotenv import load_dotenv
    return load_dotenv(path or workspace_root() / '.env', override=override)
