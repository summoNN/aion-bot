from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from bot.cogs.events import EventsCog
from bot.services.event_sources import load_event_config


class FakeResponse:
    def __init__(self) -> None:
        self.deferred = False

    def is_done(self) -> bool:
        return self.deferred

    async def defer(self, *, ephemeral: bool) -> None:
        self.deferred = True

    async def send_message(self, content: str, *, ephemeral: bool) -> None:
        self.deferred = True


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, content: str, *, ephemeral: bool) -> None:
        self.messages.append(content)


def test_test_announcement_defers_before_tts_and_playback() -> None:
    async def run() -> None:
        guild = SimpleNamespace(id=123)
        interaction = SimpleNamespace(
            guild=guild,
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        voice = SimpleNamespace(
            has_target=lambda current_guild: True,
            announce=lambda current_guild, audio_path: asyncio.sleep(0),
        )
        tts = SimpleNamespace(synthesize=lambda text: asyncio.sleep(0, result=Path("test.mp3")))
        cog = EventsCog(
            bot=SimpleNamespace(),
            events_path=Path("config/events.json"),
            default_timezone="UTC",
            voice_service=voice,
            tts_service=tts,
        )
        cog.scheduler._config = load_event_config(Path("config/events.json"))

        await cog._test_announcement(
            interaction,
            SimpleNamespace(value="rift", name="Rift Time"),
        )

        assert interaction.response.deferred is True
        assert interaction.followup.messages == ["Played the `Spacetime Rift` test announcement."]

    asyncio.run(run())
