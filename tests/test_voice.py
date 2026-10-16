from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from bot.services.voice import VoiceService
from bot.services.voice_config import VoiceConfigStore


def test_new_voice_target_is_persistent_autojoin() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "voice.json"
        store = VoiceConfigStore(path)
        config = store.set_channel(123, 456)
        assert config.autojoin is True
        reloaded = VoiceConfigStore(path).get(123)
        assert reloaded is not None and reloaded.channel_id == 456 and reloaded.autojoin is True


def test_intentional_leave_suppresses_reconnect() -> None:
    class FakeClient:
        channel = None

        def is_playing(self) -> bool:
            return False

        async def disconnect(self, force: bool = False) -> None:
            return None

    async def run() -> None:
        with TemporaryDirectory() as directory:
            store = VoiceConfigStore(Path(directory) / "voice.json")
            store.set_channel(1, 2)
            service = VoiceService("ffmpeg", store)
            guild = SimpleNamespace(id=1, voice_client=FakeClient())
            service._reconnect_enabled.add(1)
            await service.disconnect(guild, intentional=True)
            service.request_reconnect(guild, reason="test")
            assert 1 not in service._reconnect_enabled
            assert 1 not in service._retry_tasks
            await service.close()

    asyncio.run(run())


def test_voice_queue_serializes_playback() -> None:
    async def run() -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "announcement.mp3"
            audio.write_bytes(b"test")
            store = VoiceConfigStore(root / "voice.json")
            store.set_channel(1, 2)
            service = VoiceService("ffmpeg", store)
            guild = SimpleNamespace(id=1, voice_client=None)
            active = 0
            maximum = 0
            order: list[str] = []

            async def fake_play(current_guild, request) -> None:
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                order.append(request.audio_path.name)
                await asyncio.sleep(0.01)
                active -= 1

            service._play_now = fake_play
            await asyncio.gather(service.announce(guild, audio), service.announce(guild, audio))
            assert maximum == 1
            assert order == ["announcement.mp3", "announcement.mp3"]
            await service.close()

    asyncio.run(run())
