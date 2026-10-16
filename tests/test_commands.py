from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from main import AionBot


def test_join_and_channel_command_schemas_are_not_confused() -> None:
    async def run() -> None:
        settings = SimpleNamespace(
            events_path=Path("config/events.json"),
            default_timezone="UTC",
            ffmpeg_executable="ffmpeg",
            voice_volume=1.0,
            voice_config_path=Path("config/voice_channels.json"),
            tts_voice="en-US-AriaNeural",
            tts_cache_dir=Path("cache/tts"),
            health_path=Path("cache/health/test-command-ready"),
            guild_id=None,
        )
        bot = AionBot(settings)
        await bot.setup_hook()
        try:
            join = bot.tree.get_command("join")
            setchannel = bot.tree.get_command("setchannel")
            assert join is not None
            assert join.parameters == []
            assert setchannel is not None
            assert [parameter.name for parameter in setchannel.parameters] == ["channel"]
        finally:
            await bot.close()

    asyncio.run(run())
