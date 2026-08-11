# -*- coding: utf-8 -*-
"""
Minimal .env loader.

Deliberately dependency-free and deliberately non-overriding: a variable
already present in the real environment wins, so a shell export can be used
without editing the file.

Values are never logged. Callers read them through os.environ.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config


def load_dotenv(path: Path | None = None) -> int:
    """Load KEY=VALUE lines into os.environ. Returns how many were set."""
    dotenv_path = Path(path or config.DOTENV_PATH)
    if not dotenv_path.exists():
        return 0

    loaded = 0
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or not value:
            continue
        if key in os.environ and os.environ[key].strip():
            continue

        os.environ[key] = value
        loaded += 1

    return loaded
