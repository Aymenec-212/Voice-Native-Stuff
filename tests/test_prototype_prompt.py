"""The GO prompt (docs/PLAN.md §7).

Driven with real simulated keystrokes, because the bug this replaces was exactly the kind
a shape-only test misses: `readline.set_startup_hook` imported fine on macOS and silently
did nothing, so the line came up empty and Enter cancelled the run.
"""

import pytest

from vnr.cli.prototype import _edit_transcript_fallback, edit_transcript

TRANSCRIPT = "find recent work on streaming ASR for Darija"

ENTER = "\r"
CLEAR_LINE = "\x15"  # ctrl-u
BACKSPACE = "\x7f"


async def run_prompt(keys: str, text: str = TRANSCRIPT) -> str | None:
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        with create_app_session(input=pipe, output=DummyOutput()):
            return await edit_transcript(text)


async def test_the_line_is_prefilled_so_enter_approves_the_transcript():
    """The regression: this returned "" on macOS, which the caller read as cancel."""
    assert await run_prompt(ENTER) == TRANSCRIPT


async def test_the_transcript_can_be_edited_in_place():
    assert await run_prompt(" please" + ENTER) == TRANSCRIPT + " please"


async def test_fixing_the_tail_does_not_require_retyping_the_line():
    assert await run_prompt(BACKSPACE * 6 + "Darija please" + ENTER) == (
        "find recent work on streaming ASR for Darija please"
    )


async def test_clearing_the_line_cancels():
    """PLAN §7: empty input cancels. Approval is never the default on an empty line."""
    assert await run_prompt(CLEAR_LINE + ENTER) == ""


async def test_an_empty_transcript_stays_empty():
    assert await run_prompt(ENTER, text="") == ""


# -- the stdlib fallback, for terminals prompt_toolkit cannot drive -----------------
@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ([""], TRANSCRIPT),          # Enter approves as-is
        (["c"], None),               # explicit cancel
        (["e", "edited text"], "edited text"),
        (["E", "edited text"], "edited text"),
    ],
)
def test_fallback_menu(monkeypatch, answers, expected):
    remaining = list(answers)
    monkeypatch.setattr("builtins.input", lambda *_: remaining.pop(0))
    assert _edit_transcript_fallback(TRANSCRIPT) == expected


def test_fallback_treats_interruption_as_cancel(monkeypatch):
    def interrupt(*_):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)
    assert _edit_transcript_fallback(TRANSCRIPT) is None


async def test_prompt_falls_back_when_there_is_no_usable_terminal(monkeypatch):
    """Piped stdin must not lose the transcript."""
    import vnr.cli.prototype as prototype

    monkeypatch.setattr("builtins.input", lambda *_: "")
    monkeypatch.setattr(
        prototype, "_edit_transcript_fallback", lambda text: f"fallback:{text}"
    )

    class Boom:
        def __init__(self, **_):
            raise RuntimeError("no tty")

    monkeypatch.setattr("prompt_toolkit.PromptSession", Boom)
    assert await edit_transcript(TRANSCRIPT) == f"fallback:{TRANSCRIPT}"


# -- what GO actually sends --------------------------------------------------------
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (TRANSCRIPT, TRANSCRIPT),
        ("  spaced  ", "spaced"),
        ("", None),          # the line was cleared
        ("   ", None),       # whitespace is not approval
        (None, None),        # Ctrl-C / EOF
    ],
)
def test_approved_query(answer, expected):
    from vnr.cli.prototype import approved_query

    assert approved_query(answer) == expected
