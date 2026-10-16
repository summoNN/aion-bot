"""Persistent-target Discord voice connections and non-blocking playback queues."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable

import discord

from bot.services.voice_config import GuildVoiceConfig, VoiceConfigError, VoiceConfigStore

LOGGER = logging.getLogger(__name__)
VOICE_CHANNEL_TYPES = (discord.VoiceChannel, discord.StageChannel)
VoiceChannelLike = discord.VoiceChannel | discord.StageChannel


@dataclass(slots=True)
class PlaybackRequest:
    audio_path: Path
    volume: float
    timeout: float
    future: asyncio.Future[None]


class VoiceService:
    """Use only persisted per-guild targets and serialize all playback."""

    def __init__(
        self,
        ffmpeg_executable: str,
        voice_config: VoiceConfigStore,
        default_volume: float = 1.0,
    ) -> None:
        self.ffmpeg_executable = ffmpeg_executable
        self.voice_config = voice_config
        self._default_volume = self._validate_volume(default_volume)
        self._guild_volumes: dict[int, float] = {}
        self._target_channels: dict[int, int] = {}
        self._guilds: dict[int, discord.Guild] = {}
        self._queues: dict[int, asyncio.Queue[PlaybackRequest]] = {}
        self._workers: dict[int, asyncio.Task[None]] = {}
        self._connection_locks: dict[int, asyncio.Lock] = {}
        self._retry_tasks: dict[int, asyncio.Task[None]] = {}
        self._retry_attempts: dict[int, int] = {}
        self._retry_next_at: dict[int, float] = {}
        self._intentional_disconnects: set[int] = set()
        self._reconnect_enabled: set[int] = set()
        self._locks_guard = asyncio.Lock()
        self._closing = False

    @staticmethod
    def _validate_volume(value: float) -> float:
        if not math.isfinite(value) or not 0.0 <= value <= 2.0:
            raise ValueError("volume must be between 0.0 and 2.0")
        return round(value, 2)

    async def _connection_lock_for(self, guild_id: int) -> asyncio.Lock:
        async with self._locks_guard:
            return self._connection_locks.setdefault(guild_id, asyncio.Lock())

    def config_for(self, guild: discord.Guild) -> GuildVoiceConfig | None:
        return self.voice_config.get(guild.id)

    def volume_for(self, guild: discord.Guild) -> float:
        return self._guild_volumes.get(guild.id, self._default_volume)

    def set_volume(self, guild: discord.Guild, value: float) -> float:
        volume = self._validate_volume(value)
        self._guild_volumes[guild.id] = volume
        return volume

    def set_channel(self, guild: discord.Guild, channel: VoiceChannelLike) -> GuildVoiceConfig:
        if channel.guild.id != guild.id:
            raise VoiceConfigError("Voice channel belongs to a different guild")
        return self.voice_config.set_channel(guild.id, channel.id)

    def set_autojoin(self, guild: discord.Guild, enabled: bool) -> GuildVoiceConfig:
        config = self.voice_config.set_autojoin(guild.id, enabled)
        if enabled:
            self._reconnect_enabled.add(guild.id)
        else:
            self._reconnect_enabled.discard(guild.id)
            self._cancel_retry(guild.id)
        return config

    def target_channel(self, guild: discord.Guild) -> VoiceChannelLike | None:
        """Resolve only the persisted channel; never fall back to an arbitrary channel."""
        config = self.voice_config.get(guild.id)
        if config is None:
            return None
        channel = guild.get_channel(config.channel_id)
        return channel if isinstance(channel, VOICE_CHANNEL_TYPES) else None

    def has_configured_target(self, guild: discord.Guild) -> bool:
        return self.target_channel(guild) is not None

    def has_target(self, guild: discord.Guild) -> bool:
        """Backward-compatible name for callers that check a saved target."""
        return self.has_configured_target(guild)

    def _connected_to_target(self, guild: discord.Guild) -> bool:
        client = guild.voice_client
        config = self.voice_config.get(guild.id)
        return bool(
            client is not None
            and client.is_connected()
            and config is not None
            and client.channel is not None
            and client.channel.id == config.channel_id
        )

    def status(self, guild: discord.Guild | None) -> str:
        if guild is None:
            return "DMs are not connected to voice"
        config = self.voice_config.get(guild.id)
        volume = self.volume_for(guild)
        if config is None:
            return f"Unconfigured (volume: {volume:.2f})"
        client = guild.voice_client
        target = self.target_channel(guild)
        target_name = target.name if target else str(config.channel_id)
        connected = client is not None and client.is_connected()
        connected_name = client.channel.name if connected and client.channel else "none"
        latency = getattr(client, "latency", None) if connected else None
        latency_text = f"{latency * 1000:.0f} ms" if isinstance(latency, (int, float)) else "unknown"
        if guild.id in self._intentional_disconnects:
            reconnect_text = "suspended (intentional /leave)"
        elif guild.id in self._retry_tasks:
            attempt = self._retry_attempts.get(guild.id, 1)
            remaining = max(0, self._retry_next_at.get(guild.id, time.monotonic()) - time.monotonic())
            reconnect_text = f"retrying (attempt {attempt}, next in {remaining:.0f}s)"
        else:
            reconnect_text = "active" if guild.id in self._reconnect_enabled else "disabled"
        connection_text = f"Connected to `{connected_name}`" if connected else f"Disconnected from `{target_name}`"
        return (
            f"{connection_text} (configured: `{target_name}`, latency: {latency_text}, "
            f"reconnect: {reconnect_text}, autojoin: {config.autojoin}, volume: {volume:.2f})"
        )

    async def connect_configured(self, guild: discord.Guild, *, explicit: bool = False) -> discord.VoiceClient:
        """Connect exactly to the saved channel, with a lock preventing duplicates."""
        if self._closing:
            raise RuntimeError("Voice service is shutting down")
        if guild.id in self._intentional_disconnects and not explicit:
            raise RuntimeError("Voice connection was intentionally closed; use /join to request it again")
        config = self.voice_config.get(guild.id)
        if config is None:
            raise VoiceConfigError("No voice channel is configured for this guild")
        channel = guild.get_channel(config.channel_id)
        if not isinstance(channel, VOICE_CHANNEL_TYPES):
            raise VoiceConfigError(f"Configured channel {config.channel_id} is not available in this guild")
        if explicit:
            self._intentional_disconnects.discard(guild.id)
            self._reconnect_enabled.add(guild.id)
            self._cancel_retry(guild.id)
        lock = await self._connection_lock_for(guild.id)
        async with lock:
            self._guilds[guild.id] = guild
            bot_member = guild.me
            if bot_member is not None:
                permissions = channel.permissions_for(bot_member)
                missing = [
                    name
                    for name, allowed in (("Connect", permissions.connect), ("Speak", permissions.speak))
                    if not allowed
                ]
                if missing:
                    raise VoiceConfigError(f"Missing voice permission(s): {', '.join(missing)}")

            last_error: Exception | None = None
            for attempt in range(2):
                try:
                    client = guild.voice_client
                    if client is not None and client.is_connected():
                        if client.channel and client.channel.id != channel.id:
                            await client.move_to(channel)
                        self._target_channels[guild.id] = channel.id
                        LOGGER.info("Using existing voice connection for guild %s in channel %s", guild.id, channel.id)
                        return client
                    if client is not None:
                        await client.disconnect(force=True)
                    LOGGER.info("Connecting guild %s to configured voice channel %s", guild.id, channel.id)
                    client = await channel.connect(reconnect=True, timeout=20.0)
                    self._target_channels[guild.id] = channel.id
                    LOGGER.info("Connected guild %s to voice channel %s", guild.id, channel.id)
                    return client
                except (asyncio.TimeoutError, OSError, discord.DiscordException) as exc:
                    last_error = exc
                    if attempt == 0:
                        LOGGER.warning("Voice connection failed for guild %s; retrying once: %s", guild.id, exc)
                        await asyncio.sleep(1)
            raise RuntimeError(f"Could not connect guild {guild.id} to configured voice channel") from last_error

    async def startup(self, guilds: Iterable[discord.Guild]) -> None:
        """Schedule non-blocking permanent connections for configured autojoin guilds."""
        await self.ensure_connections(guilds, reason="startup autojoin")

    async def ensure_connections(self, guilds: Iterable[discord.Guild], *, reason: str) -> None:
        """Ensure each persisted autojoin target has one reconnect task."""
        for guild in guilds:
            config = self.voice_config.get(guild.id)
            if config and config.autojoin:
                self._reconnect_enabled.add(guild.id)
                if not self._connected_to_target(guild):
                    self.request_reconnect(guild, reason=reason)

    def request_reconnect(self, guild: discord.Guild, *, reason: str) -> None:
        if self._closing or guild.id in self._intentional_disconnects or guild.id not in self._reconnect_enabled:
            return
        if self._connected_to_target(guild):
            return
        current = self._retry_tasks.get(guild.id)
        if current is not None and not current.done():
            return
        LOGGER.info("Scheduling voice reconnect for guild %s (%s)", guild.id, reason)
        self._retry_tasks[guild.id] = asyncio.create_task(
            self._reconnect_loop(guild),
            name=f"voice-reconnect-{guild.id}",
        )

    async def _reconnect_loop(self, guild: discord.Guild) -> None:
        delay = 1.0
        attempt = 0
        try:
            while not self._closing and guild.id not in self._intentional_disconnects:
                try:
                    attempt += 1
                    self._retry_attempts[guild.id] = attempt
                    self._retry_next_at[guild.id] = time.monotonic() + delay
                    await asyncio.sleep(delay)
                    await self.connect_configured(guild)
                    LOGGER.info("Voice reconnect succeeded for guild %s", guild.id)
                    return
                except asyncio.CancelledError:
                    raise
                except (VoiceConfigError, RuntimeError, OSError, asyncio.TimeoutError, discord.DiscordException) as exc:
                    LOGGER.warning("Voice reconnect failed for guild %s: %s; retrying in %.0fs", guild.id, exc, delay * 2)
                    delay = min(delay * 2, 300.0)
        finally:
            current = self._retry_tasks.get(guild.id)
            if current is asyncio.current_task():
                self._retry_tasks.pop(guild.id, None)
                self._retry_attempts.pop(guild.id, None)
                self._retry_next_at.pop(guild.id, None)

    async def handle_voice_state_update(self, guild: discord.Guild, channel: VoiceChannelLike | None) -> None:
        """React to a bot voice disconnect unless it was intentionally requested."""
        config = self.voice_config.get(guild.id)
        if config is None or self._closing or guild.id in self._intentional_disconnects:
            return
        if guild.id not in self._reconnect_enabled:
            return
        if channel is None or channel.id != config.channel_id:
            LOGGER.warning("Unexpected voice disconnection or channel change in guild %s", guild.id)
            self.request_reconnect(guild, reason="unexpected voice state change")

    async def announce(self, guild: discord.Guild, audio_path: Path, timeout: float = 120.0) -> None:
        """Queue audio for the configured guild channel and await completion."""
        if self._closing:
            raise RuntimeError("Voice service is shutting down")
        if guild.id in self._intentional_disconnects:
            raise RuntimeError("Voice connection was intentionally closed; use /join to request it again")
        if not audio_path.is_file():
            raise FileNotFoundError(f"TTS audio file does not exist: {audio_path}")
        if self.voice_config.get(guild.id) is None:
            raise VoiceConfigError("No voice channel is configured for this guild")
        self._guilds[guild.id] = guild
        queue = self._queues.setdefault(guild.id, asyncio.Queue())
        worker = self._workers.get(guild.id)
        if worker is None or worker.done():
            worker = asyncio.create_task(self._playback_worker(guild), name=f"voice-playback-{guild.id}")
            self._workers[guild.id] = worker
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await queue.put(PlaybackRequest(audio_path, self.volume_for(guild), timeout, future))
        await future

    async def _playback_worker(self, guild: discord.Guild) -> None:
        queue = self._queues[guild.id]
        while True:
            request = await queue.get()
            try:
                if request.future.cancelled():
                    continue
                await self._play_now(guild, request)
                if not request.future.done():
                    request.future.set_result(None)
            except asyncio.CancelledError:
                if not request.future.done():
                    request.future.cancel()
                raise
            except Exception as exc:
                LOGGER.exception("Voice playback failed in guild %s", guild.id)
                if not request.future.done():
                    request.future.set_exception(exc)
            finally:
                queue.task_done()

    async def _play_now(self, guild: discord.Guild, request: PlaybackRequest) -> None:
        client = await self.connect_configured(guild)
        wait_started = asyncio.get_running_loop().time()
        while client.is_playing():
            if asyncio.get_running_loop().time() - wait_started > request.timeout:
                raise RuntimeError("Existing voice playback did not finish in time")
            await asyncio.sleep(0.1)

        finished = asyncio.Event()
        loop = asyncio.get_running_loop()
        playback_error: list[Exception] = []

        def after(error: Exception | None) -> None:
            if error is not None:
                playback_error.append(error)
            loop.call_soon_threadsafe(finished.set)

        source: discord.AudioSource | None = None
        playback_source: discord.AudioSource | None = None
        try:
            source = discord.FFmpegPCMAudio(str(request.audio_path), executable=self.ffmpeg_executable)
            playback_source = discord.PCMVolumeTransformer(source, volume=request.volume)
            client.play(playback_source, after=after)
            await asyncio.wait_for(finished.wait(), timeout=request.timeout)
        except asyncio.TimeoutError as exc:
            if client.is_playing():
                client.stop()
            raise RuntimeError(f"Audio playback exceeded {request.timeout:.0f} seconds") from exc
        finally:
            if playback_source is not None:
                playback_source.cleanup()
            elif source is not None:
                source.cleanup()
        if playback_error:
            raise RuntimeError("Discord voice playback failed") from playback_error[0]

    async def disconnect(self, guild: discord.Guild, *, intentional: bool = True) -> None:
        if intentional:
            self._intentional_disconnects.add(guild.id)
            self._reconnect_enabled.discard(guild.id)
            self._cancel_retry(guild.id)
        queue = self._queues.get(guild.id)
        if queue is not None:
            while not queue.empty():
                request = queue.get_nowait()
                queue.task_done()
                if not request.future.done():
                    request.future.set_exception(RuntimeError("Voice playback cancelled by disconnect"))
        client = guild.voice_client
        if client is not None:
            if client.is_playing():
                client.stop()
            await client.disconnect(force=True)
        self._target_channels.pop(guild.id, None)
        LOGGER.info("Disconnected guild %s from voice (intentional=%s)", guild.id, intentional)

    def _cancel_retry(self, guild_id: int) -> None:
        task = self._retry_tasks.pop(guild_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def close(self) -> None:
        self._closing = True
        for task in self._retry_tasks.values():
            task.cancel()
        for task in self._workers.values():
            task.cancel()
        tasks = [*self._retry_tasks.values(), *self._workers.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for guild in self._guilds.values():
            client = guild.voice_client
            if client is not None:
                try:
                    await client.disconnect(force=True)
                except discord.DiscordException:
                    LOGGER.exception("Could not disconnect voice client in guild %s", guild.id)
        self._connection_locks.clear()
        self._target_channels.clear()
        self._guilds.clear()
        self._queues.clear()
        self._workers.clear()
        self._retry_tasks.clear()
        self._retry_attempts.clear()
        self._retry_next_at.clear()
        self._reconnect_enabled.clear()
        self._intentional_disconnects.clear()
