"""Restart-safe, per-guild voice channel configuration."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
CONFIG_VERSION = 1


class VoiceConfigError(ValueError):
    """Raised when persistent voice configuration is invalid."""


@dataclass(frozen=True, slots=True)
class GuildVoiceConfig:
    """The approved voice target and startup behavior for one guild."""

    channel_id: int
    autojoin: bool = False


class VoiceConfigStore:
    """Load and atomically persist per-guild voice settings."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._guilds: dict[int, GuildVoiceConfig] = {}
        self.load()

    def load(self) -> None:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._guilds = {}
            LOGGER.info("Voice configuration %s does not exist; starting empty", self.path)
            return
        except json.JSONDecodeError as exc:
            raise VoiceConfigError(f"Invalid JSON in {self.path}: {exc.msg} at line {exc.lineno}") from exc
        except OSError as exc:
            raise VoiceConfigError(f"Could not read voice configuration {self.path}: {exc}") from exc

        if not isinstance(document, dict) or document.get("version", CONFIG_VERSION) != CONFIG_VERSION:
            raise VoiceConfigError(f"Unsupported voice configuration in {self.path}")
        raw_guilds = document.get("guilds", {})
        if not isinstance(raw_guilds, dict):
            raise VoiceConfigError("voice configuration guilds must be an object")

        guilds: dict[int, GuildVoiceConfig] = {}
        for raw_guild_id, raw_config in raw_guilds.items():
            try:
                guild_id = int(raw_guild_id)
            except (TypeError, ValueError) as exc:
                raise VoiceConfigError(f"Invalid guild ID in voice configuration: {raw_guild_id}") from exc
            if guild_id <= 0 or not isinstance(raw_config, dict):
                raise VoiceConfigError(f"Invalid voice configuration for guild {raw_guild_id}")
            channel_id = raw_config.get("channel_id")
            autojoin = raw_config.get("autojoin", False)
            if isinstance(channel_id, bool) or not isinstance(channel_id, int) or channel_id <= 0:
                raise VoiceConfigError(f"Invalid channel ID for guild {guild_id}")
            if not isinstance(autojoin, bool):
                raise VoiceConfigError(f"autojoin must be boolean for guild {guild_id}")
            guilds[guild_id] = GuildVoiceConfig(channel_id=channel_id, autojoin=autojoin)
        self._guilds = guilds
        LOGGER.info("Loaded voice configuration for %d guild(s) from %s", len(guilds), self.path)

    def get(self, guild_id: int) -> GuildVoiceConfig | None:
        return self._guilds.get(guild_id)

    def set_channel(self, guild_id: int, channel_id: int) -> GuildVoiceConfig:
        self._validate_ids(guild_id, channel_id)
        current = self._guilds.get(guild_id)
        # A newly configured permanent channel should be joined on startup.
        # Existing administrators can still opt out explicitly with /autojoin disable.
        updated = GuildVoiceConfig(channel_id=channel_id, autojoin=current.autojoin if current else True)
        self._guilds[guild_id] = updated
        self.save()
        return updated

    def set_autojoin(self, guild_id: int, enabled: bool) -> GuildVoiceConfig:
        if guild_id <= 0 or not isinstance(enabled, bool):
            raise VoiceConfigError("Invalid guild ID or autojoin value")
        current = self._guilds.get(guild_id)
        if current is None:
            raise VoiceConfigError("Set a voice channel before changing autojoin")
        updated = GuildVoiceConfig(channel_id=current.channel_id, autojoin=enabled)
        self._guilds[guild_id] = updated
        self.save()
        return updated

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(
                    {
                        "version": CONFIG_VERSION,
                        "guilds": {
                            str(guild_id): {
                                "channel_id": config.channel_id,
                                "autojoin": config.autojoin,
                            }
                            for guild_id, config in sorted(self._guilds.items())
                        },
                    },
                    temporary,
                    indent=2,
                )
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.replace(self.path)
            LOGGER.info("Persisted voice configuration atomically to %s", self.path)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise VoiceConfigError(f"Could not persist voice configuration {self.path}: {exc}") from exc

    @staticmethod
    def _validate_ids(guild_id: int, channel_id: int) -> None:
        if (
            isinstance(guild_id, bool)
            or not isinstance(guild_id, int)
            or guild_id <= 0
            or isinstance(channel_id, bool)
            or not isinstance(channel_id, int)
            or channel_id <= 0
        ):
            raise VoiceConfigError("Guild and channel IDs must be positive integers")
