"""external@1: a verifier that is a command you supply (task-specific checks,
e.g. "does this code render", plug in here from outside the scraper).

Protocol: the row as one JSON object on stdin (UTF-8, newline-terminated);
the command prints {"verdict": "pass" | "fail" | "unknown", "reasons": [..]}
on stdout and exits 0. Anything else -- a non-zero exit, unreadable or
oversized output, a timeout -- is `unknown`, never a pass.

How it runs (the scraper never executes generated code itself):
  - asyncio.create_subprocess_exec, argv list, NO shell;
  - its own session / process group (start_new_session), so on timeout the
    whole group is killed by the child's own PID (os.killpg(pid)) -- never by
    name or command line;
  - a fresh empty temporary working directory (also its HOME and TMPDIR),
    removed afterwards;
  - a minimal environment: PATH and LANG only -- no API keys, no other
    variables from the scraper's environment;
  - stdout capped at config.EXTERNAL_VERIFIER_MAX_STDOUT_BYTES (over: killed,
    unknown), stderr at 2 KB (only its tail is reported);
  - timeout: options.timeout_seconds (default
    config.EXTERNAL_VERIFIER_DEFAULT_TIMEOUT, at most
    config.EXTERNAL_VERIFIER_MAX_TIMEOUT);
  - at most `settings.external_verifier_concurrency` external verifier
    processes at once per run.

**An external verifier that runs generated code must sandbox it itself**
(container, VM, seccomp/sandbox-exec, no network, resource limits). The
isolation above keeps the scraper's secrets and files out of reach of a
well-behaved command; it is not a sandbox for hostile code.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import tempfile

import config
from verifiers import VERDICTS, PageState, Verdict, Verifier

STDERR_MAX = 2048


class _TooLarge(Exception):
    pass


async def _read_capped(stream, cap: int, raise_over: bool) -> bytes:
    data = b""
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return data
        if len(data) < cap + 1:
            data += chunk[: cap + 1 - len(data)]
        if len(data) > cap and raise_over:
            raise _TooLarge


class ExternalV1(Verifier):
    name, version = "external", 1
    external = True

    def __init__(self, options: dict | None = None):
        options = dict(options or {})
        command = options.pop("command", None)
        timeout = options.pop("timeout_seconds", config.EXTERNAL_VERIFIER_DEFAULT_TIMEOUT)
        record_types = options.pop("record_types", None)
        if options:
            raise ValueError(f"verifier {self.id} unknown options: {sorted(options)}")
        if not isinstance(command, list) or not command or not all(isinstance(a, str) and a for a in command):
            raise ValueError(f"verifier {self.id} needs options.command: a non-empty list of strings (argv, no shell)")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not (
                0 < timeout <= config.EXTERNAL_VERIFIER_MAX_TIMEOUT):
            raise ValueError(f"verifier {self.id} timeout_seconds must be in (0, {config.EXTERNAL_VERIFIER_MAX_TIMEOUT}]")
        if record_types is not None and (not isinstance(record_types, list)
                                         or not all(isinstance(t, str) and t for t in record_types)):
            raise ValueError(f"verifier {self.id} record_types must be a list of record type names")
        self.command, self.timeout = list(command), float(timeout)
        self.record_types = tuple(record_types) if record_types is not None else None

    async def verify(self, row: dict, page: PageState) -> Verdict:
        workdir = await asyncio.to_thread(tempfile.mkdtemp, prefix="scraper-verifier-")
        try:
            return await self._run(row, workdir)
        finally:
            await asyncio.to_thread(shutil.rmtree, workdir, True)

    async def _run(self, row: dict, workdir: str) -> Verdict:
        env = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8", "HOME": workdir, "TMPDIR": workdir}
        payload = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=workdir, env=env, start_new_session=True)
        except OSError as e:
            return Verdict.unknown(f"could not start the command: {type(e).__name__}: {e.strerror or e}")

        async def talk():
            async def feed():
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    proc.stdin.write(payload)
                    await proc.stdin.drain()
                    proc.stdin.close()
            out, err, _ = await asyncio.gather(
                _read_capped(proc.stdout, config.EXTERNAL_VERIFIER_MAX_STDOUT_BYTES, True),
                _read_capped(proc.stderr, STDERR_MAX, False), feed())
            return out, err, await proc.wait()

        try:
            out, err, code = await asyncio.wait_for(talk(), self.timeout)
        except TimeoutError:
            await self._kill(proc)
            return Verdict.unknown(f"timed out after {self.timeout:g}s")
        except _TooLarge:
            await self._kill(proc)
            return Verdict.unknown(f"stdout over {config.EXTERNAL_VERIFIER_MAX_STDOUT_BYTES} bytes")
        if code != 0:
            tail = err.decode("utf-8", "replace").strip()[-200:]
            return Verdict.unknown(f"exit code {code}" + (f": {tail}" if tail else ""))
        try:
            answer = json.loads(out.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return Verdict.unknown("stdout is not a JSON verdict")
        verdict = answer.get("verdict") if isinstance(answer, dict) else None
        reasons = answer.get("reasons", []) if isinstance(answer, dict) else None
        if verdict not in VERDICTS or not isinstance(reasons, list):
            return Verdict.unknown('stdout must be {"verdict": "pass"|"fail"|"unknown", "reasons": [...]}')
        return Verdict(verdict, tuple(str(r)[:300] for r in reasons[:20]))

    @staticmethod
    async def _kill(proc) -> None:
        """Kill the child's own process group (its PID is the group id: start_new_session)."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):  # drain to EOF so the pipes and transport close
            await asyncio.wait_for(proc.communicate(), 5)
