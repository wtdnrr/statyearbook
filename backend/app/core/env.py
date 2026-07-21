from __future__ import annotations

import os
from pathlib import Path


def load_local_env_file() -> None:
    """Load repository-local settings without overwriting process variables."""

    env_path = Path(__file__).resolve().parents[3] / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_value(primary: str, legacy: str | None = None, *, default: str = "") -> str:
    fallback = os.getenv(legacy, default) if legacy else default
    return os.getenv(primary, fallback).strip()


def env_flag(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
