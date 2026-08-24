"""Repo/data-dir layout the server assumes, plus the one place that
resolves a run_id (a URL path segment, so untrusted) into an actual
filesystem path -- every route goes through run_dir_for() rather than
joining request input into a path itself, so a run_id can't walk out of
data/ (e.g. "..", an absolute path, or a path separator).
"""
from __future__ import annotations

import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")
PYTHON = sys.executable  # the same interpreter/venv the server itself runs under

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def is_valid_run_id(run_id: str) -> bool:
    return bool(run_id) and run_id not in (".", "..") and bool(_RUN_ID_RE.match(run_id))


def run_dir_for(run_id: str) -> str:
    if not is_valid_run_id(run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")
    path = os.path.join(DATA_DIR, run_id)
    resolved = os.path.realpath(path)
    data_real = os.path.realpath(DATA_DIR)
    if resolved != data_real and not resolved.startswith(data_real + os.sep):
        raise ValueError(f"run_id escapes data dir: {run_id!r}")
    return path


def new_run_id(mode: str, host: str) -> str:
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_host = re.sub(r"[^A-Za-z0-9.-]", "-", host)
    return f"run-{mode}-{safe_host}-{ts}"
