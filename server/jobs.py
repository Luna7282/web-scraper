"""Subprocess-based job tracking: the mechanism behind every long-running
operation the server exposes (starting/resuming a map or full crawl,
export). The server process itself never runs crawl4ai, never opens the
async Frontier, never touches chromadb -- every one of those goes through
`python main.py ...` / `python -m export.export ...` as a separate OS
process (see main.py's headless_map/headless_crawl and ARCHITECTURE
history: the event-loop-starvation incidents that killed a shared
crawl4ai browser are exactly why this pipeline never runs inside a
long-lived server loop with other work).

Restart survival, stated precisely: state lives in <run_dir>/job.json
(pid, kind, started_at, status) -- read fresh from disk and cross-checked
against the OS on every query, never cached across a server restart. This
is a PID file, not a DB lock row (see LESSONS_LEARNED.md for the
reasoning): a lock row would need the subprocess to actively heartbeat it,
and staleness still has to fall back to the same OS liveness check either
way, so the DB row would add bookkeeping without adding certainty.

Windows-specific liveness/kill (this project runs on Windows -- see
CLAUDE.md): `tasklist`/`taskkill` rather than os.kill's Windows semantics,
which don't cleanly support a liveness-only signal-0-style check.
"""
from __future__ import annotations

import json
import os
import warnings

# subprocess.Popen warns "process is still running" when a Popen object is
# garbage-collected without wait()/poll() ever confirming it exited -- the
# expected, correct outcome for a job we launch specifically to keep
# running after start() returns (a map/crawl/export can run for hours; we
# are never going to wait() on it here). Not a leak: no fd/handle we own
# stays open (see start()'s log_f.close() right after Popen), only the
# warning itself is a false positive for this deliberately-detached-child
# usage pattern.
warnings.filterwarnings("ignore", category=ResourceWarning, module="subprocess")
import subprocess
import sys
from datetime import datetime, timezone

from server.paths import PYTHON, REPO_ROOT

JOB_FILE = "job.json"
LOG_FILE = "job.log"
CONFIG_FILE = "run_config.json"

_CREATIONFLAGS = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=5
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def _kill_pid(pid: int) -> None:
    if os.name != "nt":
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        return
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
    except Exception:
        pass


_EXIT_MARKER = "__job_exit__"


def _wrap_with_exit_marker(cmd: list[str]) -> list[str]:
    """Every launched job (headless_run OR export -- different processes,
    different stdout conventions, export's own dataset_card JSON doesn't
    even share headless_run's {counts_by_status, fatal_errors} shape)
    needs ONE reliable, job-kind-agnostic success/failure signal. Sniffing
    stdout for a specific JSON shape is fragile by construction -- it only
    recognizes the one format it was written against (found the hard way:
    export jobs always reported "exited", never "done", because
    dataset_card doesn't have those keys). The actual reliable signal is
    the process's real exit code, which this process doesn't otherwise
    have access to after a server restart (no held Popen handle -- see the
    class docstring). So: wrap the real command in a tiny outer `python
    -c` that runs it as a child, waits, and prints one unambiguous marker
    line with the real return code -- independent of whatever the inner
    command does or doesn't print.
    """
    inner = json.dumps(cmd)
    code = (
        "import json, subprocess, sys; "
        f"rc = subprocess.call(json.loads({inner!r})); "
        f"print({_EXIT_MARKER!r} + str(rc)); "
        "sys.exit(rc)"
    )
    return [PYTHON, "-u", "-c", code]


class JobError(Exception):
    pass


class JobManager:
    """Genuinely stateless: every query re-derives from job.json + the OS
    (tasklist/os.kill), never from anything held in memory -- there is no
    in-process cache of the launched Popen object. Deliberate, not an
    oversight: keeping one would need active reaping (Popen.wait() /
    .poll(), or Python warns about an unreaped child at GC time) for a
    benefit this class never uses get_status() always asks the OS
    directly, restart or not, so a cached handle would only be a second,
    redundant source of truth to keep in sync with the first.
    """

    def start(self, run_dir: str, kind: str, cmd: list[str], config: dict | None = None) -> dict:
        existing = self.get_status(run_dir)
        if existing is not None and existing["state"] == "running":
            raise JobError(f"a job is already running for {run_dir}")
        os.makedirs(run_dir, exist_ok=True)
        if config is not None:
            config = dict(config)
            config.setdefault("started_at", _now_iso())
            _write_json(os.path.join(run_dir, CONFIG_FILE), config)

        log_path = os.path.join(run_dir, LOG_FILE)
        log_f = open(log_path, "ab")
        try:
            popen = subprocess.Popen(
                _wrap_with_exit_marker(cmd), cwd=REPO_ROOT, stdout=log_f, stderr=subprocess.STDOUT,
                creationflags=_CREATIONFLAGS,
            )
        finally:
            log_f.close()  # the child inherited its own handle at spawn time; ours isn't needed after that

        record = {
            "kind": kind, "pid": popen.pid, "started_at": _now_iso(),
            "cmd": cmd, "status": "running",
        }
        _write_json(os.path.join(run_dir, JOB_FILE), record)
        return self.get_status(run_dir)

    def _tail(self, run_dir: str, max_bytes: int = 200_000) -> str | None:
        log_path = os.path.join(run_dir, LOG_FILE)
        if not os.path.exists(log_path):
            return None
        try:
            with open(log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(size - max_bytes if size > max_bytes else 0)
                chunk = f.read()
            return chunk.decode("utf-8", errors="replace")
        except Exception:
            return None

    def _exit_code(self, run_dir: str) -> int | None:
        """The authoritative done-vs-failed signal -- see
        _wrap_with_exit_marker's docstring for why this exists instead of
        sniffing stdout shape. None means the marker line was never
        written: the wrapper itself never got to run or finish printing
        it (killed via stop(), or a crash severe enough to take the
        wrapper down too)."""
        text = self._tail(run_dir)
        if text is None:
            return None
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith(_EXIT_MARKER):
                try:
                    return int(line[len(_EXIT_MARKER):])
                except ValueError:
                    return None
        return None

    def _final_summary(self, run_dir: str) -> dict | None:
        """Best-effort: the last JSON object in the log before the exit
        marker, whatever shape it is -- headless_run's own
        {counts_by_status, fatal_errors} summary (one line), export's
        dataset_card (json.dumps(..., indent=2) -- many lines), or
        nothing at all if the command never printed JSON. This is purely
        informational (the 'result' field); done vs failed vs exited
        comes from _exit_code(), not from finding or not finding a
        summary here.

        Multi-line JSON means single-line parsing isn't enough: try
        growing suffixes (last 1 line, last 2, ...) up to a bound, newest
        first, and take the first one that parses -- cheap for a status
        check that already tails at most 200KB, and correct regardless of
        whether the command pretty-printed or not.
        """
        text = self._tail(run_dir)
        if text is None:
            return None
        lines = [ln for ln in text.splitlines() if not ln.strip().startswith(_EXIT_MARKER)]
        for n in range(1, min(len(lines), 500) + 1):
            candidate = "\n".join(lines[-n:]).strip()
            if not candidate.startswith("{"):
                continue
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        return None

    def get_status(self, run_dir: str) -> dict | None:
        record = _read_json(os.path.join(run_dir, JOB_FILE))
        if record is None:
            return None
        record = dict(record)
        alive = _pid_alive(record["pid"])
        if alive:
            record["state"] = "running"
            record["result"] = None
        elif record.get("status") == "stopped":
            record["state"] = "stopped"
            record["result"] = self._final_summary(run_dir)
        else:
            rc = self._exit_code(run_dir)
            if rc is None:
                record["state"] = "exited"  # crashed hard enough to take the wrapper down too
            elif rc == 0:
                record["state"] = "done"
            else:
                record["state"] = "failed"
            record["result"] = self._final_summary(run_dir)
            record["returncode"] = rc
        return record

    def stop(self, run_dir: str) -> dict:
        """Kills the job's process -- but only marks the record 'stopped'
        if it actually had to. A job can legitimately finish on its own
        (hit max_pages, quiesced) in the gap between a caller deciding to
        stop it and this call landing; taskkill-ing an already-dead pid is
        a harmless no-op, but unconditionally overwriting status here
        would relabel a genuine natural completion as a deliberate stop,
        losing the real 'done'/'failed' outcome get_status() would
        otherwise have reported from the process's own summary line.
        """
        record = _read_json(os.path.join(run_dir, JOB_FILE))
        if record is None:
            raise JobError(f"no job recorded for {run_dir}")
        if _pid_alive(record["pid"]):
            _kill_pid(record["pid"])
            record["status"] = "stopped"
            _write_json(os.path.join(run_dir, JOB_FILE), record)
        return self.get_status(run_dir)

    def resolved_config(self, run_dir: str) -> dict | None:
        return _read_json(os.path.join(run_dir, CONFIG_FILE))


def run_bounded(args: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    """For short, synchronous CLI calls the server makes within one
    request/response cycle (discover, cancel/select, pause/unpause,
    dataset-report, query) -- bounded and bypassed by the job-polling
    mechanism entirely, since none of these are 'long' in the sense the
    brief means (they're one small subprocess launch, not an hours-long
    crawl). Still a separate process, same reasoning as JobManager's
    module docstring: the server never runs this code in its own loop.
    """
    return subprocess.run(
        [PYTHON, "-u"] + args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout,
    )
