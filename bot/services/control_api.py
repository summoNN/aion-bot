"""Authenticated HTTP control API used by the web dashboard."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import discord
from aiohttp import web

from bot.services.event_sources import EventConfigError, build_event, event_to_dict
from bot.services.voice import VOICE_CHANNEL_TYPES, VoiceService
from bot.services.voice_config import VoiceConfigError

LOGGER = logging.getLogger(__name__)


class ControlAPI:
    """Expose only the bot controls required by the dashboard.

    The dashboard talks to this service server-to-server. Every control route
    therefore requires the shared bearer token; the health route is public so
    a hosting platform can probe the process without receiving that token.
    """

    def __init__(
        self,
        bot: discord.Client,
        events_cog: Any,
        voice_service: VoiceService,
        tts_service: Any,
        token: str,
        host: str = "0.0.0.0",
        port: int = 8765,
    ) -> None:
        self.bot = bot
        self.events_cog = events_cog
        self.voice_service = voice_service
        self.tts_service = tts_service
        self.token = token
        self.host = host
        self.port = port
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        if self._runner is not None:
            return
        app = web.Application()
        app.router.add_get("/api/health", self._health)
        app.router.add_get("/api/v1/guilds", self._guilds)
        app.router.add_get("/api/v1/state/{guild_id}", self._state)
        app.router.add_patch("/api/v1/config/{guild_id}", self._config)
        app.router.add_post("/api/v1/actions/{guild_id}", self._action)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        try:
            self._site = web.TCPSite(self._runner, self.host, self.port)
            await self._site.start()
        except Exception:
            await self._runner.cleanup()
            self._runner = None
            raise
        LOGGER.info("Control API listening on %s:%s", self.host, self.port)

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            LOGGER.info("Control API stopped")

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "ready": self.bot.is_ready(),
                "guildCount": len(self.bot.guilds),
            }
        )

    async def _guilds(self, request: web.Request) -> web.Response:
        unauthorized = self._authorize(request)
        if unauthorized is not None:
            return unauthorized
        return web.json_response({"guilds": [self._guild_payload(guild) for guild in self.bot.guilds]})

    async def _state(self, request: web.Request) -> web.Response:
        unauthorized = self._authorize(request)
        if unauthorized is not None:
            return unauthorized
        guild = self._find_guild(request.match_info["guild_id"])
        if guild is None:
            return self._error("Bot is not installed in this server.", 404)
        return web.json_response(self._state_payload(guild))

    async def _config(self, request: web.Request) -> web.Response:
        unauthorized = self._authorize(request)
        if unauthorized is not None:
            return unauthorized
        guild = self._find_guild(request.match_info["guild_id"])
        if guild is None:
            return self._error("Bot is not installed in this server.", 404)
        try:
            body = await request.json()
        except (TypeError, ValueError):
            return self._error("Request body must be valid JSON.", 400)
        if not isinstance(body, dict):
            return self._error("Request body must be an object.", 400)

        try:
            await self._apply_config(guild, body)
        except (EventConfigError, VoiceConfigError, ValueError, RuntimeError) as exc:
            return self._error(str(exc), 400)
        except (discord.DiscordException, OSError, asyncio.TimeoutError) as exc:
            LOGGER.exception("Control API configuration update failed for guild %s", guild.id)
            return self._error(str(exc), 502)
        return web.json_response(self._state_payload(guild))

    async def _action(self, request: web.Request) -> web.Response:
        unauthorized = self._authorize(request)
        if unauthorized is not None:
            return unauthorized
        guild = self._find_guild(request.match_info["guild_id"])
        if guild is None:
            return self._error("Bot is not installed in this server.", 404)
        try:
            body = await request.json()
        except (TypeError, ValueError):
            return self._error("Request body must be valid JSON.", 400)
        if not isinstance(body, dict) or not isinstance(body.get("action"), str):
            return self._error("action is required.", 400)

        try:
            await self._run_action(guild, body)
        except (EventConfigError, VoiceConfigError, ValueError, RuntimeError, asyncio.TimeoutError) as exc:
            return self._error(str(exc), 400)
        except (discord.DiscordException, OSError) as exc:
            LOGGER.exception("Control API action failed for guild %s", guild.id)
            return self._error(str(exc), 502)
        return web.json_response(self._state_payload(guild))

    async def _apply_config(self, guild: discord.Guild, body: dict[str, Any]) -> None:
        scheduler = self.events_cog.scheduler
        config = scheduler.config
        if config is None:
            raise RuntimeError("The event scheduler is not ready yet.")

        if any(key in body for key in ("eventId", "enabled", "messages")):
            event_id = body.get("eventId")
            if not isinstance(event_id, str) or not event_id.strip():
                raise EventConfigError("eventId is required when changing an event or its messages.")
            event = config.by_id(event_id)
            if event is None:
                raise EventConfigError(f"Unknown event id: {event_id}")
            enabled = body.get("enabled", event.enabled)
            messages = body.get("messages", {})
            if not isinstance(enabled, bool):
                raise EventConfigError("enabled must be a boolean.")
            if not isinstance(messages, dict):
                raise EventConfigError("messages must be an object.")
            validated = build_event(
                event_id=event.id,
                name=event.name,
                region=event.region,
                timezone_name=event.timezone_name,
                weekdays=list(event.schedule.weekdays),
                times=[value.strftime("%H:%M") for value in event.schedule.times],
                reminders_minutes=list(event.schedule.reminders_minutes),
                enabled=enabled,
                start_message=messages.get("start", event.messages.start),
                reminder_message=messages.get("reminder", event.messages.reminder),
                source=event.source,
                verification_status=event.verification_status,
            )
            await scheduler.upsert_event(replace(event, enabled=enabled, messages=validated.messages))
            config = scheduler.config

        if "channelId" in body:
            channel_id = body["channelId"]
            if isinstance(channel_id, bool) or not isinstance(channel_id, int) or channel_id <= 0:
                raise VoiceConfigError("channelId must be a positive integer.")
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, VOICE_CHANNEL_TYPES):
                raise VoiceConfigError("Select a voice or stage channel from this server.")
            self.voice_service.set_channel(guild, channel)
            current_voice_config = self.voice_service.config_for(guild)
            if current_voice_config and current_voice_config.autojoin:
                await self.voice_service.connect_configured(guild, explicit=True)

        if "autojoin" in body:
            autojoin = body["autojoin"]
            if not isinstance(autojoin, bool):
                raise VoiceConfigError("autojoin must be a boolean.")
            self.voice_service.set_autojoin(guild, autojoin)
            if autojoin:
                await self.voice_service.connect_configured(guild, explicit=True)

        if "volume" in body:
            volume = body["volume"]
            if isinstance(volume, bool) or not isinstance(volume, (int, float)):
                raise ValueError("volume must be a number between 0.0 and 2.0")
            self.voice_service.set_volume(guild, float(volume))

    async def _run_action(self, guild: discord.Guild, body: dict[str, Any]) -> None:
        action = body["action"]
        if action == "connect":
            await self.voice_service.connect_configured(guild, explicit=True)
            return
        if action == "disconnect":
            await self.voice_service.disconnect(guild, intentional=True)
            return
        if action != "test":
            raise ValueError(f"Unknown action: {action}")

        event_id = body.get("eventId")
        config = self.events_cog.scheduler.config
        event = config.by_id(event_id) if config is not None and isinstance(event_id, str) else None
        if event is None:
            raise EventConfigError("A valid eventId is required for a test announcement.")
        if not self.voice_service.has_target(guild):
            raise VoiceConfigError("No saved voice target is available for this server.")
        text = event.messages.start.format(name=event.name, minutes=0)
        audio_path = await self.tts_service.synthesize(text)
        await self.voice_service.announce(guild, audio_path)

    def _state_payload(self, guild: discord.Guild) -> dict[str, Any]:
        config = self.events_cog.scheduler.config
        events = []
        if config is not None:
            jobs = self.events_cog.scheduler.job_descriptions()
            for event in config.events:
                event_payload = event_to_dict(event)
                starts = [job for job in jobs if job["event_id"] == event.id and job["reminder_minutes"] == 0]
                starts.sort(key=lambda job: str(job.get("next_run") or ""))
                event_payload["next_start"] = starts[0].get("next_run") if starts else None
                events.append(event_payload)
        return {
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "bot": {
                "status": "online" if self.bot.is_ready() else "starting",
                "id": str(self.bot.user.id) if self.bot.user else None,
                "name": str(self.bot.user) if self.bot.user else "AION 2 Announce Bot",
                "guildCount": len(self.bot.guilds),
                "schedulerRunning": bool(self.events_cog.scheduler.running),
            },
            "guild": self._guild_payload(guild),
            "voice": self._voice_payload(guild),
            "schedule": {
                "internalTimezone": config.internal_timezone_name if config else "UTC",
                "displayTimezone": config.display_timezone_name if config else "UTC",
                "events": events,
                "nextJobs": self.events_cog.scheduler.job_descriptions(),
            },
        }

    def _voice_payload(self, guild: discord.Guild) -> dict[str, Any]:
        config = self.voice_service.config_for(guild)
        target = self.voice_service.target_channel(guild)
        client = guild.voice_client
        channels_by_id: dict[int, dict[str, Any]] = {}
        for channel in guild.channels:
            if isinstance(channel, VOICE_CHANNEL_TYPES):
                channels_by_id[channel.id] = {
                    "id": channel.id,
                    "name": channel.name,
                    "categoryName": channel.category.name if channel.category else None,
                }
        connected = bool(client is not None and client.is_connected())
        latency = getattr(client, "latency", None) if connected else None
        if guild.id in self.voice_service._intentional_disconnects:
            reconnect = "suspended"
        elif guild.id in self.voice_service._retry_tasks:
            reconnect = "retrying"
        elif guild.id in self.voice_service._reconnect_enabled:
            reconnect = "active"
        else:
            reconnect = "disabled"
        return {
            "configured": config is not None,
            "connected": connected,
            "channelId": config.channel_id if config else None,
            "channelName": target.name if target else (client.channel.name if connected and client.channel else None),
            "channels": list(channels_by_id.values()),
            "autojoin": config.autojoin if config else False,
            "volume": self.voice_service.volume_for(guild),
            "latencyMs": round(latency * 1000) if isinstance(latency, (int, float)) else None,
            "reconnect": reconnect,
        }

    def _guild_payload(self, guild: discord.Guild) -> dict[str, Any]:
        member = guild.me
        return {
            "id": str(guild.id),
            "name": guild.name,
            "iconUrl": str(guild.icon.url) if guild.icon else None,
            "botHighestRolePosition": member.top_role.position if member else 0,
            "rolePositions": {str(role.id): role.position for role in guild.roles},
        }

    def _find_guild(self, value: str) -> discord.Guild | None:
        try:
            guild_id = int(value)
        except ValueError:
            return None
        return self.bot.get_guild(guild_id)

    def _authorize(self, request: web.Request) -> web.Response | None:
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return self._error("Unauthorized.", 401)
        return None

    @staticmethod
    def _error(message: str, status: int) -> web.Response:
        return web.json_response({"error": message}, status=status)
