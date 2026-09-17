"""Engine selection (docs/PLAN.md §5).

The mock engine must be asked for explicitly. Falling back to it silently when the real
runtime fails would be the same mistake as quietly swapping in the BF16 MLX checkpoint.
"""

from __future__ import annotations

from ..config import AsrConfig
from ..errors import ConfigError
from .engine import AsrEngine

KNOWN_ENGINES = ("moshicpp", "mock")


def create_engine(config: AsrConfig) -> AsrEngine:
    if config.engine == "moshicpp":
        from .moshicpp import MoshiCppEngine

        return MoshiCppEngine(config)
    if config.engine == "mock":
        from .mock import MockAsrEngine

        return MockAsrEngine(sample_rate=config.sample_rate)
    raise ConfigError(
        f"Unknown VNR_ASR_ENGINE {config.engine!r}. Known engines: {', '.join(KNOWN_ENGINES)}"
    )
