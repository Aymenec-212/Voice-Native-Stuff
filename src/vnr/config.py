"""Environment-backed configuration (docs/PLAN.md §8, §11, §23).

Secrets live in the environment or a gitignored ``.env`` — never in code. Config objects
load without credentials so that unit tests and the mock ASR engine work on any machine;
the credential check happens in :meth:`NebiusConfig.require_key` at the point of use.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .errors import ConfigError

Env = Mapping[str, str]

DEFAULT_NEBIUS_BASE_URL = "https://api.studio.nebius.com/v1"
DEFAULT_NEBIUS_MODEL = "nvidia/Nemotron-3_5-Lightning"
DEFAULT_TAVILY_BASE_URL = "https://api.tavily.com"

# Tavily bills 1 credit for a basic/fast search and 2 for an advanced one (PLAN §10).
CREDITS_PER_DEPTH = {"basic": 1, "advanced": 2}


def _str(env: Env, key: str, default: str = "") -> str:
    return (env.get(key) or default).strip()


def _int(env: Env, key: str, default: int) -> int:
    raw = _str(env, key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _float(env: Env, key: str, default: float) -> float:
    raw = _str(env, key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc


def _json_object(env: Env, key: str) -> dict[str, Any]:
    raw = _str(env, key)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{key} must be valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"{key} must be a JSON object")
    return parsed


def _bool(env: Env, key: str, default: bool) -> bool:
    raw = _str(env, key).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be a boolean, got {raw!r}")


@dataclass(frozen=True)
class NebiusConfig:
    api_key: str = ""
    base_url: str = DEFAULT_NEBIUS_BASE_URL
    model: str = DEFAULT_NEBIUS_MODEL
    timeout_s: float = 90.0
    #: Escape hatch for model-specific request fields (e.g. turning reasoning off) without
    #: a code change. Set NEBIUS_EXTRA_BODY to a JSON object.
    extra_body: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Env) -> NebiusConfig:
        return cls(
            api_key=_str(env, "NEBIUS_API_KEY"),
            base_url=_str(env, "NEBIUS_BASE_URL", DEFAULT_NEBIUS_BASE_URL).rstrip("/"),
            model=_str(env, "NEBIUS_MODEL", DEFAULT_NEBIUS_MODEL),
            timeout_s=_float(env, "RESEARCH_REQUEST_TIMEOUT_S", 90.0),
            extra_body=_json_object(env, "NEBIUS_EXTRA_BODY"),
        )

    def require_key(self) -> str:
        if not self.api_key:
            raise ConfigError(
                "NEBIUS_API_KEY is not set. Copy .env.example to .env and fill it in.",
                user_message="Nebius API key is missing.",
            )
        return self.api_key


@dataclass(frozen=True)
class TavilyConfig:
    api_key: str = ""
    base_url: str = DEFAULT_TAVILY_BASE_URL
    timeout_s: float = 30.0

    @classmethod
    def from_env(cls, env: Env) -> TavilyConfig:
        return cls(
            api_key=_str(env, "TAVILY_API_KEY"),
            base_url=_str(env, "TAVILY_BASE_URL", DEFAULT_TAVILY_BASE_URL).rstrip("/"),
            timeout_s=_float(env, "TAVILY_REQUEST_TIMEOUT_S", 30.0),
        )

    def require_key(self) -> str:
        if not self.api_key:
            raise ConfigError(
                "TAVILY_API_KEY is not set. Copy .env.example to .env and fill it in.",
                user_message="Search unavailable — Tavily API key is missing.",
            )
        return self.api_key


@dataclass(frozen=True)
class ResearchBudget:
    """Hard limits on the agent loop (PLAN §11, §14). Configuration, not constants."""

    max_turns: int = 6
    max_searches: int = 4
    max_results_per_search: int = 5
    max_advanced_searches: int = 1
    default_depth: str = "basic"
    allow_advanced: bool = True
    decision_max_tokens: int = 1024
    answer_max_tokens: int = 2048

    def __post_init__(self) -> None:
        for name in ("max_turns", "max_searches", "max_results_per_search"):
            if getattr(self, name) < 1:
                raise ConfigError(f"{name} must be >= 1")
        if self.default_depth not in CREDITS_PER_DEPTH:
            raise ConfigError(
                f"default_depth must be one of {sorted(CREDITS_PER_DEPTH)}, "
                f"got {self.default_depth!r}"
            )

    @classmethod
    def from_env(cls, env: Env) -> ResearchBudget:
        depth = _str(env, "RESEARCH_DEFAULT_DEPTH", "basic").lower()
        # The plan writes "basic or fast"; Tavily's parameter value is "basic".
        if depth == "fast":
            depth = "basic"
        return cls(
            max_turns=_int(env, "RESEARCH_MAX_TURNS", 6),
            max_searches=_int(env, "RESEARCH_MAX_SEARCHES", 4),
            max_results_per_search=_int(env, "RESEARCH_MAX_RESULTS_PER_SEARCH", 5),
            max_advanced_searches=_int(env, "RESEARCH_MAX_ADVANCED_SEARCHES", 1),
            default_depth=depth,
            allow_advanced=_bool(env, "RESEARCH_ALLOW_ADVANCED", True),
            decision_max_tokens=_int(env, "RESEARCH_DECISION_MAX_TOKENS", 1024),
            answer_max_tokens=_int(env, "RESEARCH_ANSWER_MAX_TOKENS", 2048),
        )


@dataclass(frozen=True)
class AsrConfig:
    """Milestone 1 runtime selection (PLAN §5, §6)."""

    engine: str = "moshicpp"
    binary: str = ""
    model_path: str = ""
    sample_rate: int = 24_000
    frame_ms: int = 80

    @classmethod
    def from_env(cls, env: Env) -> AsrConfig:
        return cls(
            engine=_str(env, "VNR_ASR_ENGINE", "moshicpp").lower(),
            binary=_str(env, "VNR_ASR_BINARY"),
            model_path=_str(env, "VNR_ASR_MODEL"),
            sample_rate=_int(env, "VNR_ASR_SAMPLE_RATE", 24_000),
            frame_ms=_int(env, "VNR_ASR_FRAME_MS", 80),
        )

    @property
    def frame_samples(self) -> int:
        return int(self.sample_rate * self.frame_ms / 1000)


@dataclass(frozen=True)
class ServiceConfig:
    """Loopback only — never bind the LAN (PLAN §19)."""

    host: str = "127.0.0.1"
    port: int = 8765

    @classmethod
    def from_env(cls, env: Env) -> ServiceConfig:
        host = _str(env, "VNR_SERVICE_HOST", "127.0.0.1")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ConfigError(f"VNR_SERVICE_HOST must stay on loopback, got {host!r}")
        return cls(host=host, port=_int(env, "VNR_SERVICE_PORT", 8765))


@dataclass(frozen=True)
class Settings:
    nebius: NebiusConfig = field(default_factory=NebiusConfig)
    tavily: TavilyConfig = field(default_factory=TavilyConfig)
    budget: ResearchBudget = field(default_factory=ResearchBudget)
    asr: AsrConfig = field(default_factory=AsrConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)

    @classmethod
    def load(cls, env: Env | None = None, *, dotenv_path: Path | str | None = None) -> Settings:
        """Load settings, reading a ``.env`` file first unless *env* is supplied."""
        if env is None:
            load_dotenv(dotenv_path, override=False)
            env = os.environ
        return cls(
            nebius=NebiusConfig.from_env(env),
            tavily=TavilyConfig.from_env(env),
            budget=ResearchBudget.from_env(env),
            asr=AsrConfig.from_env(env),
            service=ServiceConfig.from_env(env),
        )
