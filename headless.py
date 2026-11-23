"""Headless contract v1 (Brief 3A): a versioned, strict config that fully
describes a run.

Which contract a --headless-config file uses is decided by one key:

  * no "contract_version"   -> LEGACY headless, exactly today's behaviour
    (the control server's configs: extra keys like root_url ignored,
    behaviour from the environment, exit 0/1). Nothing in this module runs.
  * "contract_version": 1   -> v1: unknown keys refused, behaviour settings in
    the config, an API key named by `api_key_env`, distinct exit codes.
  * any other version       -> config error (exit 2).

Behaviour settings precedence (v1): config "settings" value, then the
environment variable, then the default. The effective values and where each
came from are reported (result.json, Brief 3A Step 2). They are applied to the
`config` module once, at the start of a v1 run, inside `applied_settings()`,
which restores the previous values on exit -- every reader uses `config.X` at
call time, and nothing imports these settings by name.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import config

SUPPORTED_CONTRACT_VERSIONS = (1, 2)

# Exit codes for v1 runs (legacy keeps 0 / 1, and 0 on Ctrl-C).
EXIT_OK = 0
EXIT_STOPPED_EARLY = 1      # fatal_errors: dead browser, LLM breaker stop, ...
EXIT_CONFIG_ERROR = 2       # bad config: one-line message on stderr, no traceback
EXIT_INTERRUPTED = 130      # SIGINT (128 + 2)
EXIT_STOPPED_BY_SIGNAL = 143  # SIGTERM (128 + 15): stopped by request

# The keys headless_crawl/headless_map/_resolve_llm read today.
RUN_KEYS = ("mode", "run_dir", "seeds", "host_prefix_pairs", "intent", "provider", "model", "max_pages",
            "max_depth", "crawl_workers", "extract_workers", "extract_threshold", "follow_threshold",
            "build_rag", "enable_quality_scoring", "quality_threshold")
REQUIRED_KEYS = ("contract_version", "mode", "run_dir", "seeds", "host_prefix_pairs")
V1_KEYS = ("contract_version", "run_id", "resume", "settings", "api_key_env")
ALLOWED_KEYS = frozenset(RUN_KEYS + V1_KEYS)
# Contract v2 (Brief 3B) = every v1 key plus these. A v1 config using one is
# refused as an unknown key, exactly as before.
V2_KEYS = ("model_pinned_version", "dry_run")
ALLOWED_KEYS_BY_VERSION = {1: ALLOWED_KEYS, 2: frozenset(ALLOWED_KEYS | set(V2_KEYS))}
MODES = ("crawl", "map")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class HeadlessConfigError(ValueError):
    """A v1 config the scraper refuses. The message is shown as-is (no traceback)."""


@dataclass(frozen=True)
class Setting:
    config_attr: str   # attribute on the config module the run reads
    env_var: str
    kind: type         # bool / int / str
    default: object    # v1 default when neither config nor environment sets it
    allow_none: bool = False


# v1 defaults equal today's config.py defaults, except the Step 4 safe
# defaults (v1 headless only; legacy and config.py unchanged):
# max_pages_counts_failed on, llm_breaker_threshold 5, site_retry_backoff on,
# llm_unavailable_max_releases 5.
SETTINGS: dict[str, Setting] = {
    "extract_unit_concurrency": Setting("EXTRACT_UNIT_CONCURRENCY", "EXTRACT_UNIT_CONCURRENCY", int, 1),
    "max_pages_counts_failed": Setting("MAX_PAGES_COUNTS_FAILED", "MAX_PAGES_COUNTS_FAILED", bool, True),
    "llm_breaker_threshold": Setting("LLM_BREAKER_THRESHOLD", "LLM_BREAKER_THRESHOLD", int, 5),
    "llm_sdk_max_retries": Setting("LLM_SDK_MAX_RETRIES", "LLM_SDK_MAX_RETRIES", int, 2),
    "use_system_one": Setting("USE_SYSTEM_ONE", "USE_SYSTEM_ONE", bool, False),
    "system_one_provider": Setting("SYSTEM_ONE_PROVIDER", "SYSTEM_ONE_PROVIDER", str, None, allow_none=True),
    "extract_timing_log": Setting("EXTRACT_TIMING_LOG", "EXTRACT_TIMING_LOG", bool, False),
    "stop_grace_seconds": Setting("STOP_GRACE_SECONDS", "SCRAPER_STOP_GRACE_SECONDS", int, 30),
    "site_retry_backoff": Setting("SITE_RETRY_BACKOFF", "SCRAPER_SITE_RETRY_BACKOFF", bool, True),
    "llm_unavailable_max_releases": Setting("LLM_UNAVAILABLE_MAX_RELEASES", "SCRAPER_LLM_UNAVAILABLE_MAX_RELEASES",
                                            int, 5),
}
# Contract v2 only (Brief 3B). A v1 config naming one is refused as an
# unknown setting, and v1's effective settings (result.json) are unchanged.
SETTINGS_V2_ONLY: dict[str, Setting] = {
    "provider_preflight": Setting("PROVIDER_PREFLIGHT", "SCRAPER_PROVIDER_PREFLIGHT", bool, True),
    "embeddings_preflight": Setting("EMBEDDINGS_PREFLIGHT", "SCRAPER_EMBEDDINGS_PREFLIGHT", bool, True),
}
ALL_SETTINGS: dict[str, Setting] = {**SETTINGS, **SETTINGS_V2_ONLY}


def settings_for(version: int) -> dict[str, Setting]:
    return SETTINGS if version == 1 else ALL_SETTINGS


_MINIMUMS = {"extract_unit_concurrency": 1, "llm_breaker_threshold": 0, "llm_sdk_max_retries": 0,
             "stop_grace_seconds": 0, "llm_unavailable_max_releases": 0}


@dataclass(frozen=True)
class HeadlessRun:
    """A validated v1 config, ready to run."""
    cfg: dict                   # the config (secrets never stored here: only api_key_env's name)
    contract_version: int
    effective: dict             # name -> {"value": ..., "source": "config" | "env" | "default"}
    api_key_env: str | None


def is_versioned(cfg) -> bool:
    return isinstance(cfg, dict) and "contract_version" in cfg


def validate(cfg: dict, environ: Mapping[str, str] | None = None) -> HeadlessRun:
    """Validate a v1 config and resolve its settings. Raises HeadlessConfigError."""
    environ = os.environ if environ is None else environ
    if not isinstance(cfg, dict):
        raise HeadlessConfigError("config must be a JSON object")
    version = cfg.get("contract_version")
    if type(version) is not int or version not in SUPPORTED_CONTRACT_VERSIONS:
        raise HeadlessConfigError(f"unsupported contract_version {version!r}; this scraper supports "
                                  f"{list(SUPPORTED_CONTRACT_VERSIONS)} (integer)")
    unknown = sorted(set(cfg) - ALLOWED_KEYS_BY_VERSION[version])
    if unknown:
        raise HeadlessConfigError(f"unknown config keys: {', '.join(unknown)}")
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        raise HeadlessConfigError(f"missing required config keys: {', '.join(missing)}")
    if cfg["mode"] not in MODES:
        raise HeadlessConfigError(f"mode must be one of {list(MODES)}, got {cfg['mode']!r}")
    if not isinstance(cfg["run_dir"], str) or not cfg["run_dir"].strip():
        raise HeadlessConfigError("run_dir must be a non-empty string")
    _check_pairs("seeds", cfg["seeds"])
    _check_pairs("host_prefix_pairs", cfg["host_prefix_pairs"])
    if "resume" in cfg and not isinstance(cfg["resume"], bool):
        raise HeadlessConfigError("resume must be true or false")
    if "run_id" in cfg and (not isinstance(cfg["run_id"], str) or not _RUN_ID.match(cfg["run_id"])):
        raise HeadlessConfigError("run_id must be 1-128 characters: letters, digits, '.', '_', '-'")
    if "model_pinned_version" in cfg and (not isinstance(cfg["model_pinned_version"], str)
                                          or not cfg["model_pinned_version"].strip()):
        raise HeadlessConfigError("model_pinned_version must be a non-empty string")
    if "dry_run" in cfg and not isinstance(cfg["dry_run"], bool):
        raise HeadlessConfigError("dry_run must be true or false")
    if cfg.get("dry_run") and cfg.get("resume"):
        raise HeadlessConfigError("a dry pass needs its own run_dir: dry_run and resume can't be combined")
    api_key_env = cfg.get("api_key_env")
    if api_key_env is not None:
        if not isinstance(api_key_env, str) or not _ENV_NAME.match(api_key_env):
            raise HeadlessConfigError("api_key_env must be an environment variable name")
        if not (environ.get(api_key_env) or "").strip():
            raise HeadlessConfigError(f"api_key_env names {api_key_env}, which is not set or empty")
    return HeadlessRun(cfg=dict(cfg), contract_version=version,
                       effective=resolve_settings(cfg.get("settings", {}), environ, version),
                       api_key_env=api_key_env)


def _check_pairs(name: str, value) -> None:
    if not isinstance(value, list) or not value or not all(isinstance(p, list) and len(p) == 2 for p in value):
        raise HeadlessConfigError(f"{name} must be a non-empty list of [value, value] pairs")


def resolve_settings(settings, environ: Mapping[str, str], version: int = 1) -> dict:
    """config value, then environment variable, then default -- with the source."""
    if not isinstance(settings, dict):
        raise HeadlessConfigError("settings must be an object")
    known = settings_for(version)
    unknown = sorted(set(settings) - set(known))
    if unknown:
        raise HeadlessConfigError(f"unknown settings: {', '.join(unknown)}")
    effective = {}
    for name, spec in known.items():
        if name in settings:
            value, source = _check_value(name, spec, settings[name]), "config"
        elif environ.get(spec.env_var) not in (None, ""):
            value, source = _parse_env(name, spec, environ[spec.env_var]), "env"
        else:
            value, source = spec.default, "default"
        effective[name] = {"value": value, "source": source}
    return effective


def _check_value(name: str, spec: Setting, value):
    if value is None and spec.allow_none:
        return None
    if spec.kind is int and (type(value) is not int):
        raise HeadlessConfigError(f"settings.{name} must be an integer")
    if spec.kind is bool and type(value) is not bool:
        raise HeadlessConfigError(f"settings.{name} must be true or false")
    if spec.kind is str and not isinstance(value, str):
        raise HeadlessConfigError(f"settings.{name} must be a string")
    if name in _MINIMUMS and value < _MINIMUMS[name]:
        raise HeadlessConfigError(f"settings.{name} must be >= {_MINIMUMS[name]}")
    return value


def _parse_env(name: str, spec: Setting, raw: str):
    """Same parsing config.py uses at import ("true" for bools, int() for ints)."""
    raw = raw.strip()
    if spec.kind is bool:
        return raw.lower() == "true"
    if spec.kind is int:
        try:
            value = int(raw)
        except ValueError:
            raise HeadlessConfigError(f"environment variable {spec.env_var}={raw!r} is not an integer "
                                      f"(for settings.{name})") from None
        return _check_value(name, spec, value)
    return raw


@contextmanager
def applied_settings(effective: dict):
    """Write the effective settings onto the config module for the duration of
    a v1 run, then restore exactly what was there before."""
    saved = {spec.config_attr: getattr(config, spec.config_attr) for spec in ALL_SETTINGS.values()}
    try:
        for name, entry in effective.items():
            setattr(config, ALL_SETTINGS[name].config_attr, entry["value"])
        yield
    finally:
        for attr, value in saved.items():
            setattr(config, attr, value)


def settings_snapshot() -> dict:
    """The config attributes v1 settings touch (for guards and tests)."""
    return {spec.config_attr: getattr(config, spec.config_attr) for spec in ALL_SETTINGS.values()}


# ---------------------------------------------------------------------------
# Step 2: run identity, scraper version, run_dir state, result.json
# ---------------------------------------------------------------------------
REPO_DIR = Path(__file__).resolve().parent
FALLBACK_VERSION = "0.1.0"   # pyproject.toml's static version, used when git can't answer
# Files that mean "this run_dir already holds a scraper run". Anything else
# (e.g. foundry's headless_config.json and run.log) doesn't count.
STATE_FILES = ("frontier.db", "canonical.jsonl", "timings.jsonl", "result.json")
RESULT_FILE = "result.json"
ERROR_MESSAGE_MAX = 200
_ABS_PATH = re.compile(r"(?:[A-Za-z]:\\|/)(?:[^\s'\"<>|]+)")


@dataclass(frozen=True)
class RunContext:
    """What a versioned run knows about itself, passed to headless_run (legacy: None)."""
    run_id: str
    scraper_version: str
    contract_version: int = 1
    model_pinned_version: str | None = None   # v2: defaults to the model when None


def new_run_id(clock=time.time) -> str:
    return f"{datetime.fromtimestamp(clock(), UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


def scraper_version(run=subprocess.run, repo: Path = REPO_DIR) -> dict:
    """git short commit + dirty flag of this checkout, else the static version.
    `version` is the one string rows and results carry."""
    def git(*args):
        try:
            done = run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    commit = git("rev-parse", "--short", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no")
    if not commit:
        return {"commit": None, "dirty": None, "version": f"{FALLBACK_VERSION}+unknown"}
    dirty = bool(status) if status is not None else None
    return {"commit": commit, "dirty": dirty, "version": commit + ("+dirty" if dirty else "")}


def state_files(run_dir: str | Path) -> list[str]:
    run_dir = Path(run_dir)
    return [name for name in STATE_FILES if (run_dir / name).exists()]


def prepare_run_dir(cfg: dict, clock=time.time) -> str | None:
    """Enforce the resume rule and archive a previous result. Returns the
    previous run id on resume ("unknown" if it can't be read), None for a
    fresh run. Raises HeadlessConfigError."""
    run_dir = Path(cfg["run_dir"])
    found = state_files(run_dir)
    resume = cfg.get("resume", False)
    if found and not resume:
        raise HeadlessConfigError(f"run_dir already holds a scraper run ({', '.join(found)}); "
                                  f'set "resume": true to continue it, or use an empty run_dir')
    if resume and not found:
        raise HeadlessConfigError('"resume": true but run_dir holds no scraper run to resume')
    if resume and (run_dir / "estimate.json").exists():
        raise HeadlessConfigError("run_dir holds a dry pass (estimate.json); a real run needs its own run_dir")
    if not resume:
        return None
    previous = run_dir / RESULT_FILE
    prev_id = "unknown"
    if previous.exists():
        try:
            value = json.loads(previous.read_text(encoding="utf-8")).get("run_id")
            if isinstance(value, str) and _RUN_ID.match(value):
                prev_id = value
        except (OSError, ValueError, AttributeError):
            pass
        archive = run_dir / f"result.{prev_id}.json"
        if prev_id == "unknown" or archive.exists():
            archive = run_dir / f"result.{prev_id}-{datetime.fromtimestamp(clock(), UTC):%Y%m%dT%H%M%SZ}.json"
        previous.replace(archive)
    return prev_id


def repair_canonical_tail(run_dir: str | Path, run_id: str) -> dict | None:
    """On resume: if canonical.jsonl ends mid-line (a write cut short --
    possible in principle even with whole-page appends, e.g. disk full), move
    the incomplete tail into canonical.tail.<run_id>.bin and truncate the file
    to its last complete line. Never deletes data. Returns what was done, or
    None if the file was clean."""
    path = Path(run_dir) / "canonical.jsonl"
    if not path.exists():
        return None
    raw = path.read_bytes()
    if not raw or raw.endswith(b"\n"):
        return None
    keep = raw.rfind(b"\n") + 1
    sidecar = Path(run_dir) / f"canonical.tail.{run_id}.bin"
    sidecar.write_bytes(raw[keep:])
    with open(path, "r+b") as f:
        f.truncate(keep)
    return {"bytes_moved": len(raw) - keep, "moved_to": sidecar.name}


def describe_error(exc: BaseException) -> dict:
    """Exception type and a short message with absolute paths and key-shaped
    strings removed -- safe for result.json."""
    from crawl.pipeline import _SECRET_SHAPED
    message = " ".join(str(exc).split())
    message = _SECRET_SHAPED.sub("[redacted]", _ABS_PATH.sub("<path>", message))
    if len(message) > ERROR_MESSAGE_MAX:
        message = message[: ERROR_MESSAGE_MAX - 1] + "…"
    return {"type": type(exc).__name__, "message": message}


def build_result(*, outcome: str, exit_code: int, contract_version: int | None, run_id: str | None,
                 version: dict, effective: dict | None, mode: str | None, api_key_env: str | None,
                 environ: Mapping[str, str], started_at: str, run_result: dict | None = None,
                 error: dict | None = None, resumed_from_run_id: str | None = None) -> dict:
    run_result = run_result or {}
    result = {
        "contract_version": contract_version, "run_id": run_id, "mode": mode,
        "outcome": outcome, "exit_code": exit_code,
        "scraper_version": version["version"], "scraper_commit": version["commit"],
        "scraper_dirty": version["dirty"],
        "settings": effective or {},
        "counts_by_status": run_result.get("counts_by_status", {}),
        "fatal_errors": run_result.get("fatal_errors", []),
        "api_key_env": None if api_key_env is None else
        {"name": api_key_env, "set": bool((environ.get(api_key_env) or "").strip())},
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if resumed_from_run_id is not None:
        result["resumed_from_run_id"] = resumed_from_run_id
    if run_result.get("stop") is not None:
        result["signal"] = run_result.get("stopped_by_signal")
        result["stop"] = run_result["stop"]
    for key in ("sources", "preflight", "cost", "dry_run", "estimate"):   # contract v2 only
        if run_result.get(key) is not None:
            result[key] = run_result[key]
    if error is not None:
        result["error"] = error
    return result


def write_result(run_dir: str | Path, result: dict) -> bool:
    """Atomic: temp file + rename. False (never raises) if it can't be written."""
    run_dir = Path(run_dir)
    tmp = run_dir / f".{RESULT_FILE}.tmp"
    try:
        tmp.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        tmp.replace(run_dir / RESULT_FILE)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
