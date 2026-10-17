"""Explicit .env bootstrap for local Demo/CLI entrypoints; production never calls it."""
from pathlib import Path


def load_local_environment(path: Path | None = None, *, override: bool = False) -> bool:
    from dotenv import load_dotenv
    return load_dotenv(path or Path(__file__).resolve().parent.parent / '.env', override=override)
