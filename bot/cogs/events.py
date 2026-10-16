"""Discord commands and scheduled AION 2 announcement orchestration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from bot.services.event_sources import (
    ConfiguredEvent,
    EventConfigError,
    build_event,
    parse_event_id,
)
from bot.services.scheduler import EventScheduler
from bot.services.tts import TTSService
from bot.services.voice import VOICE_CHANNEL_TYPES, VoiceService
from bot.services.voice_config import VoiceConfigError

LOGGER = logging.getLogger(__name__)


def _csv(value: str, field: str, *, allow_empty: bool = False) -> list[str]:
    if allow_empty and not value.strip():
        return []
    parts = [part.strip() for part in value.split(",")]
    if not value.strip() or any(not part for part in parts):
        raise EventConfigError(f"{field} must be a comma-separated list")
    return parts


def _reminder_csv(value: str) -> list[int]:
    result: list[int] = []
    for part in _csv(value, "reminders"):
        try:
            result.append(int(part))
        except ValueError as exc:
            raise EventConfigError("reminders must contain integer minute offsets") from exc
    return result


class EventsCog(commands.Cog):
    """Expose schedule commands and dispatch reminders to joined voice targets."""

    def __init__(
        self,
        bot: commands.Bot,
        events_path: Path,
        default_timezone: str,
        voice_service: VoiceService,
        tts_service: TTSService,
    ) -> None:
        self.bot = bot
        self.voice_service = voice_service
        self.tts_service = tts_service
        self.scheduler = EventScheduler(events_path, default_timezone, self._announce_event)

    async def start(self) -> None:
        await self.scheduler.start()
        await self.voice_service.startup(self.bot.guilds)

    async def stop(self) -> None:
        await self.scheduler.stop()

    @commands.Cog.listener()
    async def on_resumed(self) -> None:
        """Re-check persisted autojoin targets after a gateway resume."""
        await self.voice_service.ensure_connections(self.bot.guilds, reason="gateway resumed")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if self.bot.user is None or member.id != self.bot.user.id:
            return
        channel = after.channel if isinstance(after.channel, VOICE_CHANNEL_TYPES) else None
        await self.voice_service.handle_voice_state_update(member.guild, channel)

    async def _announce_event(self, event: ConfiguredEvent, reminder_minutes: int, occurrence: datetime) -> None:
        if reminder_minutes == 0:
            text = event.messages.start.format(name=event.name, minutes=reminder_minutes)
        else:
            text = event.messages.reminder.format(name=event.name, minutes=reminder_minutes)
        audio_path = await self.tts_service.synthesize(text)
        announced = 0
        for guild in self.bot.guilds:
            if not self.voice_service.has_target(guild):
                continue
            try:
                await self.voice_service.announce(guild, audio_path)
                announced += 1
                LOGGER.info(
                    "Announced %s (%s minutes before start) in guild %s for %s",
                    event.id,
                    reminder_minutes,
                    guild.id,
                    occurrence.isoformat(),
                )
            except (discord.DiscordException, RuntimeError):
                LOGGER.exception("Could not announce %s in guild %s", event.id, guild.id)
        if announced == 0:
            LOGGER.warning("Event %s had no connected announcement targets", event.id)

    @app_commands.command(name="ping", description="Check whether the bot is responding.")
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(f"Pong! `{self.bot.latency * 1000:.0f} ms`", ephemeral=True)

    @app_commands.command(name="status", description="Show voice connection and scheduler status.")
    async def status(self, interaction: discord.Interaction) -> None:
        config = self.scheduler.config
        lines = [
            f"Voice: {self.voice_service.status(interaction.guild)}",
            f"Scheduler: {'running' if self.scheduler.running else 'stopped'}",
            f"Scheduled reminder jobs: `{len(self.scheduler.job_descriptions())}`",
            f"Loaded events: `{len(config.events) if config else 0}`",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="voicestatus", description="Show the persistent voice connection and reconnect status.")
    async def voicestatus(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(self.voice_service.status(interaction.guild), ephemeral=True)

    @app_commands.command(name="events", description="List configured AION 2 event schedules.")
    async def events(self, interaction: discord.Interaction) -> None:
        config = self.scheduler.config
        if config is None or not config.events:
            await interaction.response.send_message("No event schedules are configured.", ephemeral=True)
            return
        lines = [
            f"Schedule timezone: `{config.internal_timezone_name}` internally; display: `{config.display_timezone_name}`"
        ]
        for event in config.events:
            state = "enabled" if event.enabled else "disabled"
            placeholder = "; placeholder" if event.placeholder else ""
            weekdays = ", ".join(day.title() for day in event.schedule.weekdays) or "no weekdays"
            times = ", ".join(value.strftime("%H:%M") for value in event.schedule.times) or "no times"
            reminders = ", ".join(str(value) for value in event.schedule.reminders_minutes)
            lines.append(
                f"**{event.id}** — {event.name} [{state}{placeholder}]\n"
                f"Region: `{event.region}` | Timezone: `{event.timezone_name}`\n"
                f"Source: `{event.source}` | Verification: `{event.verification_status}`\n"
                f"{weekdays} at {times} | reminders: `{reminders}`"
            )
        await interaction.response.send_message("\n\n".join(lines)[:1900], ephemeral=True)

    @app_commands.command(name="next", description="Show the next scheduled reminders.")
    async def next_events(self, interaction: discord.Interaction) -> None:
        jobs = self.scheduler.job_descriptions()[:10]
        if not jobs:
            await interaction.response.send_message("There are no scheduled reminders.", ephemeral=True)
            return
        lines = []
        for job in jobs:
            reminder = "at start" if job["reminder_minutes"] == 0 else f"{job['reminder_minutes']}m before"
            lines.append(
                f"**{job['event_name']}** ({job['region']}) — {reminder}\n"
                f"`{job['next_run_display']}` ({job['display_timezone']}; internal UTC)"
            )
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    async def _test_announcement(self, interaction: discord.Interaction, event: app_commands.Choice[str]) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        configured = self.scheduler.config.by_id(f"{event.value}-time") if self.scheduler.config else None
        if configured is None:
            await interaction.response.send_message(
                f"The `{event.name}` event is not configured.", ephemeral=True
            )
            return
        if not self.voice_service.has_target(interaction.guild):
            await interaction.response.send_message(
                "No saved voice target is available. Use `/setvoicechannel` first.",
                ephemeral=True,
            )
            return
        text = configured.messages.start.format(name=configured.name, minutes=0)
        try:
            audio_path = await self.tts_service.synthesize(text)
            await self.voice_service.announce(interaction.guild, audio_path)
        except (asyncio.TimeoutError, OSError, discord.DiscordException, RuntimeError, ValueError) as exc:
            LOGGER.exception("Test voice announcement failed")
            await interaction.response.send_message(f"Voice test failed: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(f"Played the `{configured.name}` test announcement.", ephemeral=True)

    @app_commands.command(name="testannouncement", description="Speak a test announcement for Rift or Shugo Time.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(
        event=[
            app_commands.Choice(name="Rift Time", value="rift"),
            app_commands.Choice(name="Shugo Time", value="shugo"),
        ]
    )
    async def testannouncement(self, interaction: discord.Interaction, event: app_commands.Choice[str]) -> None:
        await self._test_announcement(interaction, event)

    @app_commands.command(name="testvoice", description="Speak a test voice announcement for Rift or Shugo Time.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(
        event=[
            app_commands.Choice(name="Rift Time", value="rift"),
            app_commands.Choice(name="Shugo Time", value="shugo"),
        ]
    )
    async def testvoice(self, interaction: discord.Interaction, event: app_commands.Choice[str]) -> None:
        await self._test_announcement(interaction, event)

    @app_commands.command(name="join", description="Connect to this guild's saved voice channel.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def join(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        try:
            client = await self.voice_service.connect_configured(interaction.guild, explicit=True)
        except VoiceConfigError as exc:
            if str(exc) == "No voice channel is configured for this guild":
                await interaction.response.send_message(
                    "No voice channel is configured for this server. Use `/setchannel` first.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(f"Could not connect: {exc}", ephemeral=True)
            return
        except (asyncio.TimeoutError, OSError, discord.DiscordException, RuntimeError) as exc:
            LOGGER.exception("Could not connect to the saved guild voice channel")
            self.voice_service.request_reconnect(interaction.guild, reason="explicit /join failed")
            await interaction.response.send_message(f"Could not connect: {exc}", ephemeral=True)
            return
        channel_name = client.channel.name if client.channel else "saved channel"
        await interaction.response.send_message(f"Connected to `{channel_name}`.", ephemeral=True)

    @app_commands.command(name="leave", description="Disconnect from the current voice channel.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def leave(self, interaction: discord.Interaction) -> None:
        if interaction.guild is not None:
            try:
                await self.voice_service.disconnect(interaction.guild, intentional=True)
            except (asyncio.TimeoutError, discord.DiscordException) as exc:
                LOGGER.exception("Could not disconnect from voice")
                await interaction.response.send_message(f"Could not disconnect cleanly: {exc}", ephemeral=True)
                return
        await interaction.response.send_message("Disconnected from voice.", ephemeral=True)

    async def _set_voice_channel(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        if not isinstance(channel, VOICE_CHANNEL_TYPES):
            await interaction.response.send_message(
                f"`{channel.name}` is not a voice channel. Select a Discord voice channel.",
                ephemeral=True,
            )
            return
        try:
            config = self.voice_service.set_channel(interaction.guild, channel)
            if config.autojoin:
                try:
                    await self.voice_service.connect_configured(interaction.guild, explicit=True)
                except (asyncio.TimeoutError, OSError, discord.DiscordException, RuntimeError, VoiceConfigError) as exc:
                    LOGGER.exception("Saved voice channel but could not connect immediately")
                    if not isinstance(exc, VoiceConfigError):
                        self.voice_service.request_reconnect(interaction.guild, reason="voice channel updated")
                    await interaction.response.send_message(
                        f"Saved `{channel.name}`, but connection will retry in the background: {exc}",
                        ephemeral=True,
                    )
                    return
        except VoiceConfigError as exc:
            await interaction.response.send_message(f"Could not save voice channel: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(f"Saved `{channel.name}` as this guild's announcement channel.", ephemeral=True)

    @app_commands.command(name="setvoicechannel", description="Persist the voice channel used for announcements.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setvoicechannel(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel) -> None:
        await self._set_voice_channel(interaction, channel)

    @app_commands.command(name="setchannel", description="Set the persistent announcement voice channel.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setchannel(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel) -> None:
        await self._set_voice_channel(interaction, channel)

    @app_commands.command(name="autojoin", description="Enable or disable reconnecting on bot startup.")
    @app_commands.choices(
        setting=[
            app_commands.Choice(name="Enable", value="enable"),
            app_commands.Choice(name="Disable", value="disable"),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def autojoin(self, interaction: discord.Interaction, setting: app_commands.Choice[str]) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        enabled = setting.value == "enable"
        try:
            config = self.voice_service.set_autojoin(interaction.guild, enabled)
        except VoiceConfigError as exc:
            await interaction.response.send_message(f"Could not update autojoin: {exc}", ephemeral=True)
            return
        if enabled:
            try:
                await self.voice_service.connect_configured(interaction.guild, explicit=True)
            except (asyncio.TimeoutError, OSError, discord.DiscordException, RuntimeError, VoiceConfigError) as exc:
                LOGGER.exception("Autojoin enabled but initial connection failed")
                if not isinstance(exc, VoiceConfigError):
                    self.voice_service.request_reconnect(interaction.guild, reason="autojoin enabled")
                await interaction.response.send_message(
                    f"Autojoin enabled; connection will retry in the background: {exc}", ephemeral=True
                )
                return
        await interaction.response.send_message(
            f"Autojoin {'enabled' if config.autojoin else 'disabled'} for this guild.", ephemeral=True
        )

    @app_commands.command(name="volume", description="View or set announcement volume for this server.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(level="Volume from 0.0 (mute) to 2.0 (200%); omit to view current volume")
    async def volume(self, interaction: discord.Interaction, level: float | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        if level is None:
            await interaction.response.send_message(
                f"Announcement volume is `{self.voice_service.volume_for(interaction.guild):.2f}`.",
                ephemeral=True,
            )
            return
        try:
            volume = self.voice_service.set_volume(interaction.guild, level)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(f"Announcement volume set to `{volume:.2f}`.", ephemeral=True)

    @app_commands.command(name="toggleevent", description="Enable or disable Rift or Shugo Time scheduling.")
    @app_commands.choices(
        event=[
            app_commands.Choice(name="Rift Time", value="rift-time"),
            app_commands.Choice(name="Shugo Time", value="shugo-time"),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def toggleevent(
        self,
        interaction: discord.Interaction,
        event: app_commands.Choice[str],
        enabled: bool,
    ) -> None:
        configured = self.scheduler.config.by_id(event.value) if self.scheduler.config else None
        if configured is None:
            await interaction.response.send_message(f"Unknown event `{event.value}`.", ephemeral=True)
            return
        try:
            await self.scheduler.upsert_event(replace(configured, enabled=enabled))
        except EventConfigError as exc:
            await interaction.response.send_message(f"Could not update event: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"`{configured.name}` scheduling {'enabled' if enabled else 'disabled'}.", ephemeral=True
        )

    @app_commands.command(name="setevent", description="Create or replace an event schedule.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        event_id="Stable ID such as rift-time or shugo-time",
        weekdays="Comma-separated weekdays, for example monday,friday",
        times="Comma-separated local HH:MM values, for example 00:00,12:30",
        reminders="Comma-separated minutes before start; include 0, default 10,5,1,0",
        start_message="Optional start message; supports {name} and {minutes}",
        reminder_message="Optional reminder message; supports {name} and {minutes}",
        source="Optional source or provenance note",
        verification_status="Optional verification label, such as unverified or official",
    )
    async def setevent(
        self,
        interaction: discord.Interaction,
        event_id: str,
        name: str,
        region: str,
        timezone: str,
        weekdays: str,
        times: str,
        reminders: str = "10,5,1,0",
        enabled: bool = True,
        start_message: str | None = None,
        reminder_message: str | None = None,
        source: str | None = None,
        verification_status: str | None = None,
    ) -> None:
        try:
            event = build_event(
                event_id=event_id,
                name=name,
                region=region,
                timezone_name=timezone,
                weekdays=_csv(weekdays, "weekdays", allow_empty=not enabled),
                times=_csv(times, "times", allow_empty=not enabled),
                reminders_minutes=_reminder_csv(reminders),
                enabled=enabled,
                start_message=start_message,
                reminder_message=reminder_message,
                source=source or "Configured by administrator",
                verification_status=verification_status or "admin-configured",
            )
            existing = self.scheduler.config.by_id(event.id) if self.scheduler.config else None
            await self.scheduler.upsert_event(event)
        except EventConfigError as exc:
            await interaction.response.send_message(f"Invalid schedule: {exc}", ephemeral=True)
            return
        action = "updated" if existing else "created"
        await interaction.response.send_message(f"Event `{event.id}` {action} atomically.", ephemeral=True)

    @app_commands.command(name="removeevent", description="Remove an event schedule by ID.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def removeevent(self, interaction: discord.Interaction, event_id: str) -> None:
        try:
            normalized_id = parse_event_id(event_id)
            await self.scheduler.remove_event(normalized_id)
        except EventConfigError as exc:
            await interaction.response.send_message(f"Could not remove event: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(f"Event `{normalized_id}` removed.", ephemeral=True)

    @app_commands.command(name="reloadschedule", description="Reload config/events.json and rebuild jobs.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reloadschedule(self, interaction: discord.Interaction) -> None:
        try:
            await self.scheduler.reload()
        except EventConfigError as exc:
            LOGGER.exception("Event configuration reload failed")
            await interaction.response.send_message(f"Schedule validation failed: {exc}", ephemeral=True)
            return
        await interaction.response.send_message("Schedule reloaded without duplicate jobs.", ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "You need Manage Server permission to edit schedules, voice configuration, or volume."
        elif isinstance(error, app_commands.TransformerError):
            command_name = interaction.command.name if interaction.command else "unknown"
            if command_name == "join":
                message = (
                    "The deployed `/join` command is using an outdated definition. "
                    "Restart/redeploy the bot so `/join` is refreshed; it takes no channel argument."
                )
            elif command_name in {"setchannel", "setvoicechannel"}:
                message = "Select a Discord voice channel for this command, not a text or category channel."
            else:
                message = "Discord could not convert that command option. Check the selected channel and try again."
        else:
            LOGGER.exception("Application command failed", exc_info=error)
            message = "The command failed unexpectedly. Check the bot logs."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
