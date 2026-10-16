"""Application entry point for the AION 2 event announcer."""

from __future__ import annotations

import logging
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from bot.cogs.events import EventsCog
from bot.services.event_sources import EventConfigError
from bot.services.control_api import ControlAPI
from bot.services.tts import TTSService
from bot.services.voice import VoiceService
from bot.services.voice_config import VoiceConfigError, VoiceConfigStore

LOGGER = logging.getLogger(__name__)


def _parse_int(value: str | None, name: str) -> int | None:
    """Parse an optional integer environment variable with a useful error."""
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _parse_float(value: str | None, name: str, default: float) -> float:
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings loaded from environment variables."""

    token: str
    events_path: Path
    default_timezone: str
    tts_voice: str
    tts_cache_dir: Path
    ffmpeg_executable: str
    voice_volume: float
    voice_config_path: Path
    log_level: str
    guild_id: int | None
    health_path: Path
    control_api_host: str
    control_api_port: int
    control_api_token: str

    @classmethod
    def from_environment(cls) -> Settings:
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise ValueError("DISCORD_TOKEN is required")

        return cls(
            token=token,
            events_path=Path(os.getenv("EVENTS_CONFIG_PATH", "config/events.json")),
            default_timezone=os.getenv("DEFAULT_TIMEZONE", "UTC"),
            tts_voice=os.getenv("TTS_VOICE", "en-US-AriaNeural"),
            tts_cache_dir=Path(os.getenv("TTS_CACHE_DIR", "cache/tts")),
            ffmpeg_executable=os.getenv("FFMPEG_EXECUTABLE", "ffmpeg"),
            voice_volume=_parse_float(os.getenv("VOICE_VOLUME"), "VOICE_VOLUME", 1.0),
            voice_config_path=Path(os.getenv("VOICE_CONFIG_PATH", "config/voice_channels.json")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            guild_id=_parse_int(os.getenv("GUILD_ID"), "GUILD_ID"),
            health_path=Path(os.getenv("HEALTHCHECK_PATH", "cache/health/ready")),
            control_api_host=os.getenv("CONTROL_API_HOST", "0.0.0.0"),
            control_api_port=_parse_int(os.getenv("CONTROL_API_PORT", "8765"), "CONTROL_API_PORT") or 8765,
            control_api_token=os.getenv("CONTROL_API_TOKEN", "").strip(),
        )


class StructuredFormatter(logging.Formatter):
    """Emit one JSON object per log record for VPS/container aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    """Configure consistent application logging."""
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        handlers=[logging.StreamHandler()],
        force=True,
    )
    for handler in logging.getLogger().handlers:
        handler.setFormatter(StructuredFormatter())


class AionBot(commands.Bot):
    """Discord bot that owns the event and voice service lifecycle."""

    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        voice_config = VoiceConfigStore(settings.voice_config_path)
        self.voice_service = VoiceService(settings.ffmpeg_executable, voice_config, settings.voice_volume)
        self.tts_service = TTSService(settings.tts_voice, settings.tts_cache_dir)
        self.events_cog: EventsCog | None = None
        self.control_api: ControlAPI | None = None
        self._started = False

    def _mark_health(self) -> None:
        try:
            self.settings.health_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings.health_path.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            LOGGER.exception("Could not update health marker %s", self.settings.health_path)

    def _clear_health(self) -> None:
        try:
            self.settings.health_path.unlink(missing_ok=True)
        except OSError:
            LOGGER.exception("Could not clear health marker %s", self.settings.health_path)

    async def setup_hook(self) -> None:
        self.events_cog = EventsCog(
            bot=self,
            events_path=self.settings.events_path,
            default_timezone=self.settings.default_timezone,
            voice_service=self.voice_service,
            tts_service=self.tts_service,
        )
        await self.add_cog(self.events_cog)
        control_token = getattr(self.settings, "control_api_token", "")
        if control_token:
            self.control_api = ControlAPI(
                bot=self,
                events_cog=self.events_cog,
                voice_service=self.voice_service,
                tts_service=self.tts_service,
                token=control_token,
                host=getattr(self.settings, "control_api_host", "0.0.0.0"),
                port=getattr(self.settings, "control_api_port", 8765),
            )
            await self.control_api.start()
        else:
            LOGGER.warning("CONTROL_API_TOKEN is not set; dashboard control API is disabled")

    async def on_ready(self) -> None:
        if self.user is None:
            return

        if not self._started and self.events_cog is not None:
            await self._sync_commands()
            try:
                await self.events_cog.start()
            except EventConfigError:
                LOGGER.exception("Could not start because the event configuration is invalid")
                await self.close()
                return
            self._started = True
            self._mark_health()

        LOGGER.info("Logged in as %s (id=%s)", self.user, self.user.id)

    async def _sync_commands(self) -> None:
        """Synchronize commands without allowing a bad development guild to stop the bot."""
        if self.settings.guild_id is None:
            try:
                await self.tree.sync()
            except discord.HTTPException:
                LOGGER.exception("Global slash-command sync failed; the bot will continue without updated commands")
            else:
                LOGGER.info("Slash commands synced globally")
            return

        guild = discord.Object(id=self.settings.guild_id)
        self.tree.clear_commands(guild=guild)
        self.tree.copy_global_to(guild=guild)
        try:
            await self.tree.sync(guild=guild)
        except discord.Forbidden:
            LOGGER.exception(
                "Cannot sync commands to guild %s (Missing Access). Verify GUILD_ID, that the bot is in that "
                "server, and that its invite included applications.commands. Falling back to global sync.",
                self.settings.guild_id,
            )
            self.tree.clear_commands(guild=guild)
            try:
                await self.tree.sync()
            except discord.HTTPException:
                LOGGER.exception("Global fallback slash-command sync failed")
            else:
                LOGGER.info("Slash commands synced globally after development-guild access failure")
        else:
            LOGGER.info("Slash commands synced to development guild %s", self.settings.guild_id)

    async def on_resumed(self) -> None:
        self._mark_health()

    async def close(self) -> None:
        self._clear_health()
        if self.control_api is not None:
            await self.control_api.close()
        if self.events_cog is not None:
            await self.events_cog.stop()
        await self.tts_service.close()
        await self.voice_service.close()
        await super().close()


def main() -> None:
    load_dotenv()
    try:
        settings = Settings.from_environment()
        configure_logging(settings.log_level)
        bot = AionBot(settings)
        bot.run(settings.token, log_handler=None)
    except (ValueError, EventConfigError, VoiceConfigError) as exc:
        logging.basicConfig(level=logging.ERROR)
        LOGGER.error("Configuration error: %s", exc)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        LOGGER.info("Shutdown requested")


if __name__ == "__main__":
    main()
