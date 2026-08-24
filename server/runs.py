"""Run history: summarizes every data/run-* directory for the UI to pick
from. Read-only, same discipline as crawl/live_status.py -- a fresh
sqlite3 connection per call, safe to run against a frontier.db a crawl is
actively writing to.
"""
from __future__ import annotations

import json
import os

from crawl.live_status import read_live_status
from server.jobs import CONFIG_FILE, JobManager
from server.paths import DATA_DIR


def _read_json(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _count_lines(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        return sum(1 for _ in f)


def summarize_run(run_id: str, job_manager: JobManager) -> dict | None:
    run_dir = os.path.join(DATA_DIR, run_id)
    db_path = os.path.join(run_dir, "frontier.db")
    if not os.path.exists(db_path):
        return None

    cfg = _read_json(os.path.join(run_dir, CONFIG_FILE)) or {}
    status = read_live_status(db_path, recent_limit=1, in_flight_limit=1)
    canonical_path = os.path.join(run_dir, "canonical.jsonl")
    job = job_manager.get_status(run_dir)

    root_url = cfg.get("root_url")
    if root_url is None:
        seeds = cfg.get("seeds") or []
        root_url = seeds[0][0] if seeds else None

    counts = status["counts_by_status"]
    mode = cfg.get("mode")
    if mode is None:
        # No run_config.json (e.g. a run predating this server, or one
        # driven straight from the CLI) -- best-effort guess from shape:
        # 'mapped' rows only exist from map mode.
        mode = "map" if "mapped" in counts and "done" not in counts else "crawl"

    return {
        "run_id": run_id,
        "mode": mode,
        "root_url": root_url,
        "started_at": cfg.get("started_at"),
        "counts_by_status": counts,
        "pages_fetched": sum(counts.get(s, 0) for s in ("done", "skipped_extract", "mapped")),
        "pages_discovered": sum(counts.values()),
        "pair_count": _count_lines(canonical_path),
        "has_rag": os.path.isdir(os.path.join(run_dir, "chroma_db")),
        "job_state": job["state"] if job else None,
        "last_change_at": status["last_change_at"],
    }


def list_runs(job_manager: JobManager) -> list[dict]:
    if not os.path.isdir(DATA_DIR):
        return []
    out = []
    for name in sorted(os.listdir(DATA_DIR)):
        if not os.path.isdir(os.path.join(DATA_DIR, name)):
            continue
        summary = summarize_run(name, job_manager)
        if summary is not None:
            out.append(summary)
    out.sort(key=lambda r: r["started_at"] or "", reverse=True)
    return out
