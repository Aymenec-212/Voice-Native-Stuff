"""Error types shared across the application.

Each one maps to a user-visible failure mode in docs/PLAN.md §22.
"""

from __future__ import annotations


class VnrError(Exception):
    """Base class for every error this application raises deliberately."""

    #: Short, stable code sent to the UI so it can pick wording/iconography.
    code = "error"
    #: Text safe to show a user as-is.
    user_message = "Something went wrong."

    def __init__(self, message: str | None = None, *, user_message: str | None = None):
        super().__init__(message or user_message or self.user_message)
        if user_message is not None:
            self.user_message = user_message


class ConfigError(VnrError):
    code = "config"
    user_message = "Configuration is incomplete."


class NebiusError(VnrError):
    code = "nebius"
    user_message = "The reasoning model is unavailable."


class NebiusAuthError(NebiusError):
    code = "nebius_auth"
    user_message = "Nebius rejected the API key."


class TavilyError(VnrError):
    code = "tavily"
    user_message = "Search unavailable"


class TavilyAuthError(TavilyError):
    code = "tavily_auth"
    user_message = "Search unavailable — Tavily rejected the API key."


class TavilyRateLimitError(TavilyError):
    code = "tavily_rate_limit"
    user_message = "Search unavailable — Tavily rate limit reached."


class BudgetExceededError(VnrError):
    code = "budget"
    user_message = "Research budget exhausted."


class AsrUnavailableError(VnrError):
    code = "asr_unavailable"
    user_message = "Speech recognition is not ready."


class MicrophoneError(VnrError):
    code = "microphone"
    user_message = "Microphone unavailable."


class CancelledError(VnrError):
    code = "cancelled"
    user_message = "Cancelled."
