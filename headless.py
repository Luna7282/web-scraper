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

import os
import re
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass

import config

SUPPORTED_CONTRACT_VERSIONS = (1,)

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


# Step 1: v1 defaults equal today's config.py defaults. (Brief 3A Step 4
# changes some of them for v1 headless runs only.)
SETTINGS: dict[str, Setting] = {
    "extract_unit_concurrency": Setting("EXTRACT_UNIT_CONCURRENCY", "EXTRACT_UNIT_CONCURRENCY", int, 1),
    "max_pages_counts_failed": Setting("MAX_PAGES_COUNTS_FAILED", "MAX_PAGES_COUNTS_FAILED", bool, False),
    "llm_breaker_threshold": Setting("LLM_BREAKER_THRESHOLD", "LLM_BREAKER_THRESHOLD", int, 0),
    "llm_sdk_max_retries": Setting("LLM_SDK_MAX_RETRIES", "LLM_SDK_MAX_RETRIES", int, 2),
    "use_system_one": Setting("USE_SYSTEM_ONE", "USE_SYSTEM_ONE", bool, False),
    "system_one_provider": Setting("SYSTEM_ONE_PROVIDER", "SYSTEM_ONE_PROVIDER", str, None, allow_none=True),
    "extract_timing_log": Setting("EXTRACT_TIMING_LOG", "EXTRACT_TIMING_LOG", bool, False),
}
_MINIMUMS = {"extract_unit_concurrency": 1, "llm_breaker_threshold": 0, "llm_sdk_max_retries": 0}


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
    unknown = sorted(set(cfg) - ALLOWED_KEYS)
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
    api_key_env = cfg.get("api_key_env")
    if api_key_env is not None:
        if not isinstance(api_key_env, str) or not _ENV_NAME.match(api_key_env):
            raise HeadlessConfigError("api_key_env must be an environment variable name")
        if not (environ.get(api_key_env) or "").strip():
            raise HeadlessConfigError(f"api_key_env names {api_key_env}, which is not set or empty")
    return HeadlessRun(cfg=dict(cfg), contract_version=version,
                       effective=resolve_settings(cfg.get("settings", {}), environ), api_key_env=api_key_env)


def _check_pairs(name: str, value) -> None:
    if not isinstance(value, list) or not value or not all(isinstance(p, list) and len(p) == 2 for p in value):
        raise HeadlessConfigError(f"{name} must be a non-empty list of [value, value] pairs")


def resolve_settings(settings, environ: Mapping[str, str]) -> dict:
    """config value, then environment variable, then default -- with the source."""
    if not isinstance(settings, dict):
        raise HeadlessConfigError("settings must be an object")
    unknown = sorted(set(settings) - set(SETTINGS))
    if unknown:
        raise HeadlessConfigError(f"unknown settings: {', '.join(unknown)}")
    effective = {}
    for name, spec in SETTINGS.items():
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
    saved = {spec.config_attr: getattr(config, spec.config_attr) for spec in SETTINGS.values()}
    try:
        for name, spec in SETTINGS.items():
            setattr(config, spec.config_attr, effective[name]["value"])
        yield
    finally:
        for attr, value in saved.items():
            setattr(config, attr, value)


def settings_snapshot() -> dict:
    """The config attributes v1 settings touch (for guards and tests)."""
    return {spec.config_attr: getattr(config, spec.config_attr) for spec in SETTINGS.values()}
