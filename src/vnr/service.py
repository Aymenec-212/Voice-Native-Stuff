"""The local service the native app talks to (docs/PLAN.md §19).

One persistent WebSocket on loopback carries both commands and events:

    UI → service   text JSON   recording.start · recording.stop · research.submit
                               research.cancel · session.reset
                   binary      one frame of PCM (the `audio.frame` command)
    service → UI   text JSON   asr.* and research.* events, verbatim from events.py

Audio is captured by the UI and streamed over loopback — it never touches the network.
The service binds 127.0.0.1 only, and the UI never sees an API key: every external call
is made here.

The ASR model loads on explicit preparation and stays warm until the idle timeout.
Health polling and connected sockets do not keep unused weights resident.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from .asr.engine import AsrEngine
from .asr.registry import create_engine
from .config import Settings
from .controller import SETTLED, CommandRejected, ControllerDeps, SessionController
from .errors import VnrError
from .events import Event, EventType
from .logging import get_logger, log

logger = get_logger("service")

#: A desktop utility has one user and one resident model, so one connection at a time.
BUSY_CODE = 4409


def create_app(
    settings: Settings | None = None,
    *,
    engine: AsrEngine | None = None,
    deps: ControllerDeps | None = None,
) -> FastAPI:
    settings = settings or Settings.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine = engine or create_engine(settings.asr)
        app.state.settings = settings
        app.state.busy = False
        app.state.asr_error = None
        app.state.resident = False
        app.state.lock = asyncio.Lock()
        app.state.last_activity = time.monotonic()
        app.state.controller = None
        app.state.outbox = None

        stopping = asyncio.Event()

        async def expire() -> None:
            while not stopping.is_set():
                try:
                    await asyncio.wait_for(
                        stopping.wait(), timeout=min(1.0, settings.asr.idle_timeout_s / 2)
                    )
                    return
                except TimeoutError:
                    pass
                async with app.state.lock:
                    controller = app.state.controller
                    if controller is not None and controller.state not in SETTLED:
                        app.state.last_activity = time.monotonic()
                        continue
                    if (
                        app.state.resident
                        and time.monotonic() - app.state.last_activity
                        >= settings.asr.idle_timeout_s
                    ):
                        await app.state.engine.unload()
                        app.state.resident = False
                        notify_readiness()

        timer = asyncio.create_task(expire())
        try:
            yield
        finally:
            stopping.set()
            await timer
            await app.state.engine.unload()

    def notify_readiness() -> None:
        if app.state.outbox is not None:
            app.state.outbox.put_nowait(
                Event(
                    type=EventType.STATE_CHANGED,
                    data={
                        "state": app.state.controller.state.value,
                        "ready": app.state.engine.ready,
                    },
                )
            )

    app = FastAPI(title="Voice-Native Research", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ready": bool(app.state.engine.ready),
            "engine": app.state.engine.name,
            "model": settings.nebius.model,
            "error": app.state.asr_error,
        }

    @app.post("/asr/prepare")
    async def prepare() -> dict[str, Any]:
        async with app.state.lock:
            if not app.state.engine.ready:
                try:
                    if app.state.resident:
                        await app.state.engine.unload()
                        app.state.resident = False
                    await app.state.engine.load()
                    app.state.resident = True
                    app.state.asr_error = None
                except Exception as exc:
                    exc.__traceback__ = None
                    await app.state.engine.unload()
                    app.state.asr_error = (
                        exc.user_message
                        if isinstance(exc, VnrError)
                        else "The speech model could not be loaded."
                    )
                    log(logger, logging.ERROR, "asr runtime unavailable", error=str(exc))
            app.state.last_activity = time.monotonic()
            notify_readiness()
            return await health()

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        if app.state.busy:
            await websocket.close(code=BUSY_CODE, reason="another session is active")
            return
        app.state.busy = True
        try:
            await _serve(websocket, app.state.engine, settings, deps, app=app)
        finally:
            app.state.busy = False

    return app


async def _serve(
    websocket: WebSocket,
    engine: AsrEngine,
    settings: Settings,
    deps: ControllerDeps | None = None,
    *,
    app: FastAPI,
) -> None:
    # Events are pumped by their own task. Research runs in the background, so its deltas
    # must reach the UI without waiting for the UI to say something first.
    outbox: asyncio.Queue[Event] = asyncio.Queue()
    controller = SessionController(engine, settings, sink=outbox.put_nowait, deps=deps)

    app.state.controller = controller
    app.state.outbox = outbox

    async def pump() -> None:
        while True:
            event = await outbox.get()
            try:
                if websocket.client_state is not WebSocketState.CONNECTED:
                    return
                await websocket.send_text(event.to_json())
            finally:
                outbox.task_done()

    sender = asyncio.create_task(pump())
    await websocket.send_text(
        Event(
            type=EventType.STATE_CHANGED,
            data={"state": controller.state.value, "ready": engine.ready},
        ).to_json()
    )

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            try:
                async with app.state.lock:
                    app.state.last_activity = time.monotonic()
                    if (payload := message.get("bytes")) is not None:
                        await controller.push_audio(payload)
                    else:
                        await _command(controller, message.get("text") or "")
                    app.state.last_activity = time.monotonic()
            except (CommandRejected, VnrError) as exc:
                outbox.put_nowait(
                    Event(
                        type=EventType.ASR_ERROR,
                        data={"code": exc.code, "message": exc.user_message},
                        session_id=controller.session.id,
                    )
                )
    except WebSocketDisconnect:
        pass
    finally:
        async with app.state.lock:
            await controller.reset()
            app.state.controller = None
            app.state.outbox = None
            app.state.last_activity = time.monotonic()
        # Give queued events a moment to leave before the socket closes.
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(outbox.join(), timeout=0.1)
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender


async def _command(controller: SessionController, raw: str) -> None:
    try:
        message = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CommandRejected(f"malformed command: {exc.msg}") from exc
    if not isinstance(message, dict):
        raise CommandRejected("a command must be a JSON object")

    match message.get("type"):
        case "recording.start":
            await controller.start_recording()
        case "recording.stop":
            await controller.stop_recording()
        case "research.submit":
            # The UI sends the text the user approved, which may differ from the ASR's.
            await controller.submit(message.get("query"))
        case "research.cancel":
            await controller.cancel()
        case "session.reset":
            await controller.reset()
        case unknown:
            raise CommandRejected(f"unknown command {unknown!r}")


def main(argv: list[str] | None = None) -> int:
    """``uv run vnr-service`` — uvicorn bound to loopback."""
    import argparse

    import uvicorn

    from . import logging as vnr_logging

    parser = argparse.ArgumentParser(prog="vnr-service", description="Local research service")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--engine", choices=["mlx", "moshicpp", "mock"], help="override VNR_ASR_ENGINE"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    vnr_logging.configure(logging.DEBUG if args.verbose else logging.INFO)
    settings = Settings.load()
    if args.engine:
        from dataclasses import replace

        settings = replace(settings, asr=replace(settings.asr, engine=args.engine))

    host = args.host or settings.service.host
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("refusing to bind off loopback")
    uvicorn.run(create_app(settings), host=host, port=args.port or settings.service.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
