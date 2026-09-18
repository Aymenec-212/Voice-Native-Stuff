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


#: The command template for the subprocess ASR adapter. moshi.cpp takes a model
#: *directory* (-r) plus a quantization tag (-q), because the runtime needs four things:
#: the quantized LM GGUF, the Mimi codec weights, the tokenizer and config.json.
#: Adjust VNR_ASR_COMMAND to match the binary you actually built.
DEFAULT_ASR_COMMAND = "{binary} -r {model_dir} -q {quant} -i -"

#: Placeholders VNR_ASR_COMMAND may use.
COMMAND_PLACEHOLDERS = ("binary", "model_dir", "model", "quant", "sample_rate")


@dataclass(frozen=True)
class AsrConfig:
    """Milestone 1 runtime selection (PLAN §5, §6)."""

    engine: str = "mlx"
    binary: str = ""
    #: Directory holding the GGUF, the Mimi codec weights, the tokenizer and config.json.
    model_dir: str = ""
    #: A single weights file, for runtimes that want one instead of a directory.
    model_path: str = ""
    #: Quantization tag for the subprocess runtime (moshi.cpp style).
    quant: str = "q4_k"
    #: --- MLX engine ---
    #: Hub repo to fetch the four model files from when model_dir is unset.
    hf_repo: str = "kyutai/stt-1b-en_fr-mlx"
    #: Override the LM weights filename that config.json names (e.g. a .q8.safetensors).
    weights_name: str = ""
    #: Post-load quantization bits: 4 or 8. Unset infers from the weights filename.
    quant_bits: int = 0
    #: Weights in PyTorch layout need a different loader; -candle repos are detected too.
    pytorch_weights: bool = False
    max_steps: int = 4096
    sample_rate: int = 24_000
    frame_ms: int = 80
    #: argv template; see COMMAND_PLACEHOLDERS for what is substituted.
    command: str = DEFAULT_ASR_COMMAND
    #: How PCM is written to the child's stdin.
    stdin_format: str = "s16le"
    #: How the child's stdout is interpreted: "text" (streamed words) or "json" lines.
    output_format: str = "text"
    #: If set, startup waits for this string on stdout before reporting ready.
    ready_marker: str = ""
    ready_timeout_s: float = 180.0
    #: With no marker, how long the process must survive to count as started.
    ready_probe_s: float = 2.0
    #: Silence after the last output before an utterance is considered finished. Kyutai
    #: STT decodes ~0.5s behind the audio, so cutting off sooner truncates the tail.
    finalize_grace_ms: int = 800
    finalize_timeout_s: float = 10.0

    def __post_init__(self) -> None:
        if self.stdin_format not in {"s16le", "f32le"}:
            raise ConfigError(
                f"VNR_ASR_STDIN_FORMAT must be s16le or f32le, got {self.stdin_format!r}"
            )
        if self.output_format not in {"text", "json"}:
            raise ConfigError(
                f"VNR_ASR_OUTPUT_FORMAT must be text or json, got {self.output_format!r}"
            )

    @classmethod
    def from_env(cls, env: Env) -> AsrConfig:
        return cls(
            engine=_str(env, "VNR_ASR_ENGINE", "mlx").lower(),
            binary=_str(env, "VNR_ASR_BINARY"),
            model_dir=_str(env, "VNR_ASR_MODEL_DIR"),
            model_path=_str(env, "VNR_ASR_MODEL"),
            quant=_str(env, "VNR_ASR_QUANT", "q4_k"),
            hf_repo=_str(env, "VNR_ASR_HF_REPO", "kyutai/stt-1b-en_fr-mlx"),
            weights_name=_str(env, "VNR_ASR_WEIGHTS_NAME"),
            quant_bits=_int(env, "VNR_ASR_QUANT_BITS", 0),
            pytorch_weights=_bool(env, "VNR_ASR_PYTORCH_WEIGHTS", False),
            max_steps=_int(env, "VNR_ASR_MAX_STEPS", 4096),
            sample_rate=_int(env, "VNR_ASR_SAMPLE_RATE", 24_000),
            frame_ms=_int(env, "VNR_ASR_FRAME_MS", 80),
            command=_str(env, "VNR_ASR_COMMAND", DEFAULT_ASR_COMMAND),
            stdin_format=_str(env, "VNR_ASR_STDIN_FORMAT", "s16le").lower(),
            output_format=_str(env, "VNR_ASR_OUTPUT_FORMAT", "text").lower(),
            ready_marker=_str(env, "VNR_ASR_READY_MARKER"),
            ready_timeout_s=_float(env, "VNR_ASR_READY_TIMEOUT_S", 180.0),
            ready_probe_s=_float(env, "VNR_ASR_READY_PROBE_S", 2.0),
            finalize_grace_ms=_int(env, "VNR_ASR_FINALIZE_GRACE_MS", 800),
            finalize_timeout_s=_float(env, "VNR_ASR_FINALIZE_TIMEOUT_S", 10.0),
        )

    def render_command(self) -> str:
        """Fill in the argv template, naming any placeholder the template got wrong."""
        try:
            return self.command.format(
                binary=self.binary,
                model_dir=self.model_dir,
                model=self.model_path,
                quant=self.quant,
                sample_rate=self.sample_rate,
            )
        except KeyError as exc:
            raise ConfigError(
                f"VNR_ASR_COMMAND uses unknown placeholder {exc.args[0]!r}. "
                f"Available: {', '.join('{' + p + '}' for p in COMMAND_PLACEHOLDERS)}"
            ) from exc

    @property
    def frame_samples(self) -> int:
        return int(self.sample_rate * self.frame_ms / 1000)

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * (2 if self.stdin_format == "s16le" else 4)


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
