"""Live progress view for a running crawl. Read-only against the frontier
-- every call it makes (counts_by_status, in_progress_urls) only ever
SELECTs, never mutates, so polling it on any interval can't perturb the
state machine no matter how often this runs. Without this, a large crawl
with no output is indistinguishable from a hang.
"""
from __future__ import annotations

import asyncio
import time

from rich.live import Live
from rich.table import Table

from crawl.frontier import Frontier

STATUS_ORDER = [
    "queued", "pending_score", "in_progress",
    "done", "skipped_extract", "skipped_follow", "failed",
]
STATUS_STYLE = {
    "queued": "cyan", "pending_score": "cyan", "in_progress": "yellow",
    "done": "green", "skipped_extract": "dim", "skipped_follow": "dim",
    "failed": "red",
}


def _render(counts: dict[str, int], in_progress: list[str], started_at: float) -> Table:
    elapsed_minutes = max((time.monotonic() - started_at) / 60, 1e-9)
    done = counts.get("done", 0)
    rate = done / elapsed_minutes

    table = Table(title=f"Crawl progress -- {rate:.1f} pages/min")
    table.add_column("Status", style="bold")
    table.add_column("Count", justify="right")
    for status in STATUS_ORDER:
        n = counts.get(status, 0)
        if n or status in ("queued", "in_progress", "done"):
            table.add_row(status, str(n), style=STATUS_STYLE.get(status))

    if in_progress:
        table.add_row("", "")
        table.add_row("in-flight now:", "", style="yellow")
        for url in in_progress[:10]:
            table.add_row("", url, style="dim")

    return table


async def run_progress_display(frontier: Frontier, refresh_interval: float = 1.0) -> None:
    """Runs until frontier.quiescent is set. Intended to be spawned as its
    own task alongside the crawl/extract/writer workers, not awaited
    inline -- cancel it (or let it exit on its own once quiescent) same as
    any other worker task."""
    started_at = time.monotonic()
    with Live(refresh_per_second=4) as live:
        while True:
            counts = await frontier.counts_by_status()
            in_progress = await frontier.in_progress_urls()
            live.update(_render(counts, in_progress, started_at))
            if frontier.quiescent.is_set():
                return
            await asyncio.sleep(refresh_interval)
