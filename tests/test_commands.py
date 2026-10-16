from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import discord

from main import AionBot
from bot.services.voice import VOICE_CHANNEL_TYPES


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
            channel_types = getattr(setchannel.parameters[0]._Parameter__parent, "channel_types")
            assert discord.ChannelType.voice in channel_types
            assert discord.ChannelType.text in channel_types
            assert discord.VoiceChannel in VOICE_CHANNEL_TYPES
            assert discord.StageChannel in VOICE_CHANNEL_TYPES
        finally:
            await bot.close()

    asyncio.run(run())
