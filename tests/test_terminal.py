"""Terminal regressions: JSON integrity, transport failures and explicit approval."""
import argparse
import asyncio
import json

import pytest
from rich.console import Console

from vnr.cli import research_cli
from vnr.cli.main import inspect_session
from vnr.cli.prototype import Renderer, _guarded, _receive, service_url
from vnr.events import Event, EventType
from vnr.session import ResearchSession


def test_json_mode_stdout_is_one_json_document(monkeypatch, capsys):
    async def fake_run(query, settings, *, sink):
        sink(Event(EventType.RESEARCH_ANSWER_DELTA, {"text": "# Answer\nhello"}))
        sink(Event(EventType.RESEARCH_COMPLETED, {}))
        return ResearchSession(answer="# Answer\nhello"), None

    monkeypatch.setattr(research_cli, "run_research", fake_run)
    assert research_cli.main(["--json", "question"]) == 0
    assert json.loads(capsys.readouterr().out)["answer"] == "# Answer\nhello"


@pytest.mark.parametrize("host", ["example.com", "192.168.1.2", "127.0.0.1@evil.com"])
def test_audio_cannot_be_sent_off_machine(host):
    with pytest.raises(ValueError, match="local"):
        service_url(host, 8765)


def test_ipv6_loopback_url():
    assert service_url("::1", 8765) == "ws://[::1]:8765/ws"


async def test_disconnect_interrupts_wait_and_cleans_up():
    async def disconnect():
        raise ConnectionError("disconnected")

    blocked = asyncio.create_task(asyncio.Event().wait())
    receiver = asyncio.create_task(disconnect())
    with pytest.raises(ConnectionError, match="disconnected"):
        await _guarded(blocked, receiver)
    assert blocked.cancelled()


async def test_wait_timeout_is_actionable():
    with pytest.raises(TimeoutError, match="retry"):
        await _guarded(asyncio.Event().wait(), timeout=0.001)


async def test_receiver_ignores_malformed_events_and_reports_normal_close():
    class Socket:
        async def __aiter__(self):
            for data in ['[]', 'null', '{bad', '{"data": 3}',
                         '{"type":"asr.final","data":{"text":"hello"}}']:
                yield data

    renderer = Renderer(Console())
    with pytest.raises(ConnectionError, match="disconnected"):
        await _receive(Socket(), renderer)
    assert renderer.transcript == "hello"
    assert renderer.final_seen.is_set()


def test_reasoning_is_hidden_and_second_renderer_is_fresh(capsys):
    renderer = Renderer(Console())
    renderer.handle({"type": "research.reasoning_delta", "data": {"text": "private trace"}})
    assert renderer.reasoning == "private trace"
    assert "private trace" not in capsys.readouterr().out
    second = Renderer(Console())
    assert second.reasoning == second.transcript == ""
    assert not second.finished.is_set()


def test_inspect_maps_answer_numbers_to_retrieval_ids(tmp_path, capsys):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"answer": "Claim [1]", "reasoning": "hidden trace",
        "cited_sources": [{"number": 1, "id": "S3", "url": "https://example.org",
                           "title": "Evidence", "content": "Retrieved snippet"}],
        "sources": [{"id": "S3", "title": "Evidence", "url": "https://example.org",
                     "content": "Retrieved snippet"}]}))
    assert inspect_session(str(path)) == 0
    output = capsys.readouterr().out
    assert "[1] → S3" in output
    assert "Retrieved snippet" in output
    assert "hidden trace" not in output


async def test_voice_two_questions_gate_audio_on_ack_and_require_go(monkeypatch):
    """Exercise the actual client loop, with transport/microphone/keyboard substituted."""
    import httpx
    import websockets.asyncio.client

    from vnr.cli import prototype

    commands = []
    captures = []
    prompts = iter(["", "", "", "q"])  # stop, next, stop, quit
    sockets = []

    class Socket:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.acknowledged = False
            sockets.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def __aiter__(self):
            while True:
                event = await self.queue.get()
                if event["type"] == "session.state_changed":
                    self.acknowledged = True
                yield json.dumps(event)

        async def send(self, raw):
            command = json.loads(raw)
            commands.append(command)
            match command["type"]:
                case "recording.start":
                    await self.queue.put({"type": "session.state_changed",
                                          "data": {"state": "LISTENING"}})
                case "recording.stop":
                    await self.queue.put({"type": "asr.final", "data": {"text": "raw"}})
                case "research.submit":
                    await self.queue.put({"type": "research.completed", "data": {}})

    async def record(socket, settings, stop):
        assert socket.acknowledged
        captures.append(socket)
        await asyncio.Event().wait()

    async def prompt(message):
        await asyncio.sleep(0.001)
        return next(prompts)

    async def edit(text):
        assert text == "raw"
        return "approved edit"

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"ready": True}))
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kwargs: original_client(transport=transport, **kwargs))
    monkeypatch.setattr(websockets.asyncio.client, "connect", lambda *a, **k: Socket())
    monkeypatch.setattr(prototype, "_record", record)
    monkeypatch.setattr(prototype, "_prompt", prompt)
    monkeypatch.setattr(prototype, "edit_transcript", edit)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    args = argparse.Namespace(host=None, port=None, once=False)
    assert await prototype._run(args) == 0
    assert len(captures) == len(sockets) == 2
    assert [c["query"] for c in commands if c["type"] == "research.submit"] == [
        "approved edit", "approved edit"]


def test_settings_find_dotenv_in_invocation_directory(monkeypatch, tmp_path):
    from vnr.config import Settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    (tmp_path / ".env").write_text("NEBIUS_API_KEY=demo-nebius\nTAVILY_API_KEY=demo-tavily\n")
    settings = Settings.load()
    assert settings.nebius.api_key == "demo-nebius"
    assert settings.tavily.api_key == "demo-tavily"
    monkeypatch.setenv("NEBIUS_API_KEY", "environment-wins")
    assert Settings.load().nebius.api_key == "environment-wins"


async def test_asr_error_interrupts_wait_for_recording_prompt():
    class Socket:
        async def __aiter__(self):
            yield json.dumps({"type": "asr.error", "data": {"message": "Model unavailable"}})
            await asyncio.Event().wait()

    renderer = Renderer(Console())
    receiver = asyncio.create_task(_receive(Socket(), renderer))
    with pytest.raises(ConnectionError, match="Model unavailable"):
        await _guarded(asyncio.Event().wait(), receiver)


def test_terminal_markdown_and_literal_progress(monkeypatch, capsys):
    import sys

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    renderer = research_cli.TerminalRenderer(color=False)
    renderer(Event(EventType.RESEARCH_STARTED, {"query": "[bold] literal [/oops]"}))
    renderer(Event(EventType.RESEARCH_ANSWER_DELTA, {"text": "# A real heading\n\nAnswer."}))
    renderer.close()
    output = capsys.readouterr()
    assert "# A real heading" not in output.out
    assert "A real heading" in output.out
    assert "[bold] literal [/oops]" in output.err


def test_fallback_unknown_choice_never_approves(monkeypatch):
    from vnr.cli.prototype import _edit_transcript_fallback

    monkeypatch.setattr("builtins.input", lambda *_: "typo")
    assert _edit_transcript_fallback("private transcript") is None
