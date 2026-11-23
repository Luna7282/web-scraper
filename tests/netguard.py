"""Test-wide network guard (Brief 3B, after the Step 1 incident: a test that
spawned the real main.py ran a real crawl once its config became valid).

In the fast suite, any connection or DNS lookup to a non-localhost address,
and any Playwright/Chromium launch, fails the test at once -- unless the test
(method or class) is marked with @allow_network. Covers:

  - this process: socket.connect / connect_ex / sendto, socket.getaddrinfo
    (DNS: requests, httpx, asyncio all resolve through it), and subprocess
    launches of Playwright's driver or a Chromium binary (subprocess.Popen,
    asyncio.create_subprocess_exec / _shell);
  - child Python processes: install_for_suite() puts tests/netguard_site on
    PYTHONPATH, whose sitecustomize.py installs the same guard in every child
    (the incident was a child process). Children log violations to a shared
    file the parent checks after each test.

Violations are RECORDED as well as raised, and the test fails afterwards even
if the code under test swallowed the exception (e.g. a broad `except
Exception` around a fetch). Localhost (127.0.0.0/8, ::1, "localhost") and
Unix sockets stay allowed: local fake servers and closed local ports are how
these tests stay offline.

Installed by tests/test_000_network_guard.py, which discovery imports first
(modules load in sorted order before any test runs).
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest

ENV_ON, ENV_ALLOW, ENV_LOG = "SCRAPER_NETGUARD", "SCRAPER_NETGUARD_ALLOW", "SCRAPER_NETGUARD_LOG"
SITE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "netguard_site")
_BROWSER_MARKERS = ("ms-playwright", "playwright/driver", "playwright\\driver", "chromium", "chrome-headless",
                    "google chrome", "chrome.exe")

_installed = False
_allowed = False                      # set per test by the TestCase.run wrapper
violations: list[str] = []            # this process's violations since install


class NetworkBlocked(RuntimeError):
    pass


def allow_network(obj):
    """Mark a test method or class as allowed to use the network / a browser."""
    obj._network_allowed = True
    return obj


def is_local_host(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    host = str(host).strip("[]").lower()
    if host in ("localhost", "localhost.", ""):
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _violation(what: str) -> None:
    if _allowed or os.environ.get(ENV_ALLOW) == "1":
        return
    message = f"network guard: {what} (pid {os.getpid()})"
    violations.append(message)
    log = os.environ.get(ENV_LOG)
    if log:
        try:
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps(message) + "\n")
        except OSError:
            pass
    raise NetworkBlocked(message)


def _check_address(address, what: str) -> None:
    if isinstance(address, tuple) and address and not is_local_host(address[0]):
        _violation(f"{what} to {address[0]}:{address[1] if len(address) > 1 else '?'}")
    # str (AF_UNIX path) and anything else: local


def _check_argv(args, what: str) -> None:
    parts = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args or [])
    text = " ".join(os.fspath(p) if isinstance(p, os.PathLike) else
                    (p.decode(errors="replace") if isinstance(p, bytes) else str(p)) for p in parts).lower()
    if any(marker in text for marker in _BROWSER_MARKERS):
        _violation(f"{what} launching a browser: {text[:120]}")


def install() -> None:
    """Patch this process. Idempotent."""
    global _installed
    if _installed:
        return
    _installed = True

    real_connect, real_connect_ex, real_sendto = socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto
    real_getaddrinfo = socket.getaddrinfo

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _check_address(address, "connect")
        return real_connect(self, address)

    def connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _check_address(address, "connect")
        return real_connect_ex(self, address)

    def sendto(self, data, *args):
        address = args[-1] if args else None
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _check_address(address, "sendto")
        return real_sendto(self, data, *args)

    def getaddrinfo(host, *args, **kwargs):
        if not is_local_host(host):
            _violation(f"DNS lookup of {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto = connect, connect_ex, sendto
    socket.getaddrinfo = getaddrinfo

    real_popen_init = subprocess.Popen.__init__

    def popen_init(self, args, *a, **kw):
        _check_argv(args, "subprocess")
        return real_popen_init(self, args, *a, **kw)

    subprocess.Popen.__init__ = popen_init

    real_exec, real_shell = asyncio.create_subprocess_exec, asyncio.create_subprocess_shell

    async def create_subprocess_exec(program, *args, **kw):
        _check_argv([program, *args], "asyncio subprocess")
        return await real_exec(program, *args, **kw)

    async def create_subprocess_shell(cmd, **kw):
        _check_argv(cmd, "asyncio subprocess")
        return await real_shell(cmd, **kw)

    asyncio.create_subprocess_exec, asyncio.create_subprocess_shell = create_subprocess_exec, create_subprocess_shell


def read_child_log() -> list[str]:
    log = os.environ.get(ENV_LOG)
    if not log or not os.path.exists(log):
        return []
    with open(log, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def install_for_suite() -> None:
    """Guard this process, every child Python, and fail any test that tripped it."""
    install()
    if os.environ.get(ENV_ON) == "1" and os.environ.get(ENV_LOG):
        return  # already set up (e.g. imported twice)
    fd, log = tempfile.mkstemp(prefix="netguard-", suffix=".log")
    os.close(fd)
    os.environ[ENV_ON], os.environ[ENV_LOG] = "1", log
    os.environ["PYTHONPATH"] = os.pathsep.join(p for p in (SITE_DIR, os.environ.get("PYTHONPATH")) if p)
    _wrap_testcase_run()


def _is_allowed(test: unittest.TestCase) -> bool:
    method = getattr(test, getattr(test, "_testMethodName", ""), None)
    return bool(getattr(method, "_network_allowed", False) or getattr(type(test), "_network_allowed", False))


def _wrap_testcase_run() -> None:
    real_run = unittest.TestCase.run

    @functools.wraps(real_run)
    def run(self, result=None):
        global _allowed
        allowed = _is_allowed(self)
        before_local, before_child = len(violations), len(read_child_log())
        previous_env = os.environ.get(ENV_ALLOW)
        _allowed = allowed
        if allowed:
            os.environ[ENV_ALLOW] = "1"
        else:
            os.environ.pop(ENV_ALLOW, None)
        try:
            outcome = real_run(self, result)
        finally:
            _allowed = False
            if previous_env is None:
                os.environ.pop(ENV_ALLOW, None)
            else:
                os.environ[ENV_ALLOW] = previous_env
        new = violations[before_local:] + read_child_log()[before_child:]
        if new and not allowed and result is not None and not getattr(self, "_netguard_expected", False):
            try:
                raise NetworkBlocked("; ".join(new))
            except NetworkBlocked:
                result.addFailure(self, sys.exc_info())
        return outcome

    unittest.TestCase.run = run
