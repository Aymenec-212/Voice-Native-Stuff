"""Hardware check: uv run python scripts/check_asr_memory.py (Apple Silicon).

Uses the configured local ASR model, no microphone or research APIs. Reports current
RSS and MLX active/cache/peak MB before/after releasing weights, then reloads.
Peak is a historical watermark and should NOT decrease when memory is freed.
"""

import asyncio
import json

from vnr.asr.mlx_engine import MlxEngine
from vnr.config import Settings
from vnr.events import EventEmitter


async def main():
    engine = MlxEngine(Settings.load().asr)
    for cycle in range(2):
        await engine.load()
        await engine.start_session(EventEmitter())
        for _ in range(15):
            await engine.push_audio(bytes(engine.config.frame_bytes))
        await engine.finalize_session()
        print("LOADED", cycle, json.dumps(engine._backend.memory_snapshot()), flush=True)
        await engine.unload()
        print("UNLOADED", cycle, json.dumps(engine._backend.last_unload_memory), flush=True)
        memory = engine._backend.last_unload_memory
        before, after = memory["before"], memory["after"]
        assert after["mlx_active_mb"] < before["mlx_active_mb"] * 0.1
        assert after["mlx_cache_mb"] == 0
        assert after["rss_mb"] < before["rss_mb"], "RSS did not decrease"
        await asyncio.sleep(2)
        print("SETTLED", cycle, json.dumps(engine._backend.memory_snapshot()), flush=True)


asyncio.run(main())
