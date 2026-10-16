"""Validated event models and atomic JSON persistence."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import time, timezone as datetime_timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOGGER = logging.getLogger(__name__)
CONFIG_VERSION = 1
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_EVENT_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class EventConfigError(ValueError):
    """Raised when event configuration cannot be used safely."""


@dataclass(frozen=True, slots=True)
class EventSchedule:
    """Local weekly schedule and reminder offsets."""

    weekdays: tuple[str, ...]
    times: tuple[time, ...]
    reminders_minutes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class EventMessages:
    """Validated speech templates for reminders and starts."""

    reminder: str
    start: str


@dataclass(frozen=True, slots=True)
class ConfiguredEvent:
    """One region-specific event definition."""

    id: str
    name: str
    region: str
    source: str
    verification_status: str
    timezone_name: str
    timezone: tzinfo
    messages: EventMessages
    enabled: bool
    placeholder: bool
    schedule: EventSchedule


@dataclass(frozen=True, slots=True)
class EventConfig:
    """The complete versioned configuration document."""

    version: int
    internal_timezone_name: str
    internal_timezone: tzinfo
    display_timezone_name: str
    display_timezone: tzinfo
    events: tuple[ConfiguredEvent, ...]

    def by_id(self, event_id: str) -> ConfiguredEvent | None:
        normalized = event_id.strip().lower()
        return next((event for event in self.events if event.id == normalized), None)


def _message_template(value: Any, field: str, default: str, name: str) -> str:
    template = value if value is not None else default
    template = _required_string(template, field, "event", max_length=500)
    try:
        template.format(name=name, minutes=10)
    except (KeyError, IndexError, ValueError) as exc:
        raise EventConfigError(f"event.{field} may only use {{name}} and {{minutes}} placeholders") from exc
    return template


def _required_string(value: Any, field: str, context: str, max_length: int = 200) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EventConfigError(f"{context}.{field} must be a non-empty string")
    result = value.strip()
    if len(result) > max_length:
        raise EventConfigError(f"{context}.{field} is too long (maximum {max_length} characters)")
    return result


def parse_event_id(value: str, context: str = "event_id") -> str:
    """Normalize and validate a stable event ID."""
    event_id = _required_string(value, "value", context, max_length=63).lower()
    if not _EVENT_ID_RE.fullmatch(event_id):
        raise EventConfigError(f"{context} must contain only lowercase letters, numbers, and hyphens")
    return event_id


def parse_timezone(timezone_name: Any, context: str) -> tuple[str, tzinfo]:
    name = _required_string(timezone_name, "timezone", context, max_length=100)
    if name.upper() == "UTC":
        return "UTC", datetime_timezone.utc
    try:
        return name, ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise EventConfigError(f"{context}.timezone is not a valid IANA timezone: {name}") from exc


def parse_time(value: Any, context: str) -> time:
    if not isinstance(value, str):
        raise EventConfigError(f"{context} must be a local HH:MM time")
    try:
        parsed = time.fromisoformat(value.strip())
    except ValueError as exc:
        raise EventConfigError(f"{context} must be a local HH:MM time") from exc
    if parsed.tzinfo is not None or parsed.second or parsed.microsecond:
        raise EventConfigError(f"{context} must use HH:MM precision without an offset")
    return parsed


def parse_weekdays(values: Any, context: str) -> tuple[str, ...]:
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise EventConfigError(f"{context} must be an array of weekday names")
    normalized = {value.strip().lower() for value in values}
    if "" in normalized or not normalized.issubset(WEEKDAYS):
        raise EventConfigError(f"{context} contains an invalid weekday")
    return tuple(day for day in WEEKDAYS if day in normalized)


def parse_times(values: Any, context: str) -> tuple[time, ...]:
    if not isinstance(values, list):
        raise EventConfigError(f"{context} must be an array of HH:MM times")
    parsed = tuple(parse_time(value, f"{context}[{index}]") for index, value in enumerate(values))
    if len(set(parsed)) != len(parsed):
        raise EventConfigError(f"{context} contains duplicate times")
    return tuple(sorted(parsed))


def parse_reminders(values: Any, context: str) -> tuple[int, ...]:
    if not isinstance(values, list) or not values:
        raise EventConfigError(f"{context} must be a non-empty array of minute offsets")
    reminders: list[int] = []
    for index, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1440:
            raise EventConfigError(f"{context}[{index}] must be an integer from 0 through 1440")
        reminders.append(value)
    if len(set(reminders)) != len(reminders):
        raise EventConfigError(f"{context} contains duplicate reminder offsets")
    if 0 not in reminders:
        raise EventConfigError(f"{context} must include 0 for the at-start announcement")
    return tuple(sorted(reminders, reverse=True))


def parse_schedule(raw: Any, context: str, *, enabled: bool) -> EventSchedule:
    if not isinstance(raw, dict):
        raise EventConfigError(f"{context}.schedule must be an object")
    if raw.get("type", "weekly") != "weekly":
        raise EventConfigError(f"{context}.schedule.type must be 'weekly'")
    weekdays = parse_weekdays(raw.get("weekdays", raw.get("days", [])), f"{context}.schedule.weekdays")
    times = parse_times(raw.get("times", []), f"{context}.schedule.times")
    reminders = parse_reminders(
        raw.get("reminders_minutes", [10, 5, 1, 0]),
        f"{context}.schedule.reminders_minutes",
    )
    if enabled and (not weekdays or not times):
        raise EventConfigError(f"{context}.schedule needs at least one weekday and time when enabled")
    return EventSchedule(weekdays=weekdays, times=times, reminders_minutes=reminders)


def load_event_config(path: Path, default_timezone: str = "UTC") -> EventConfig:
    """Read and validate the JSON schedule document."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EventConfigError(f"Event configuration not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EventConfigError(f"Invalid JSON in {path}: {exc.msg} at line {exc.lineno}") from exc
    except OSError as exc:
        raise EventConfigError(f"Could not read event configuration {path}: {exc}") from exc

    if not isinstance(document, dict):
        raise EventConfigError("Event configuration root must be an object")
    version = document.get("version", CONFIG_VERSION)
    if version != CONFIG_VERSION:
        raise EventConfigError(f"Unsupported event configuration version: {version}")

    root_timezone_name, root_timezone = parse_timezone(document.get("timezone", default_timezone), "root")
    display_timezone_name, display_timezone = parse_timezone(
        document.get("display_timezone", "Europe/Rome"), "root.display_timezone"
    )
    raw_events = document.get("events")
    if not isinstance(raw_events, list):
        raise EventConfigError("root.events must be an array")

    events: list[ConfiguredEvent] = []
    ids: set[str] = set()
    for index, raw_event in enumerate(raw_events):
        context = f"events[{index}]"
        if not isinstance(raw_event, dict):
            raise EventConfigError(f"{context} must be an object")
        event_id = parse_event_id(raw_event.get("id"), f"{context}.id")
        if event_id in ids:
            raise EventConfigError(f"Duplicate event id: {event_id}")
        ids.add(event_id)
        enabled = raw_event.get("enabled", True)
        if not isinstance(enabled, bool):
            raise EventConfigError(f"{context}.enabled must be a boolean")
        placeholder = raw_event.get("placeholder", False)
        if not isinstance(placeholder, bool):
            raise EventConfigError(f"{context}.placeholder must be a boolean")
        if placeholder and enabled:
            raise EventConfigError(f"{context}.placeholder entries must be disabled")
        timezone_name, timezone = parse_timezone(raw_event.get("timezone", root_timezone_name), context)
        name = _required_string(raw_event.get("name"), "name", context)
        source = _required_string(
            raw_event.get("source", "Configured by administrator"), "source", context, max_length=300
        )
        verification_status = _required_string(
            raw_event.get("verification_status", "unverified"),
            "verification_status",
            context,
            max_length=80,
        )
        raw_messages = raw_event.get("messages", {})
        if not isinstance(raw_messages, dict):
            raise EventConfigError(f"{context}.messages must be an object")
        start_message = _message_template(
            raw_messages.get("start", raw_event.get("announcement")),
            "messages.start",
            "Attention Daevas! {name} has started! Good luck!",
            name,
        )
        reminder_message = _message_template(
            raw_messages.get("reminder"),
            "messages.reminder",
            "Attention Daevas! {name} starts in {minutes} minutes. Get ready!",
            name,
        )
        events.append(
            ConfiguredEvent(
                id=event_id,
                name=name,
                region=_required_string(raw_event.get("region"), "region", context, max_length=80),
                source=source,
                verification_status=verification_status,
                timezone_name=timezone_name,
                timezone=timezone,
                messages=EventMessages(reminder=reminder_message, start=start_message),
                enabled=enabled,
                placeholder=placeholder,
                schedule=parse_schedule(raw_event.get("schedule"), context, enabled=enabled),
            )
        )

    config = EventConfig(
        version=version,
        internal_timezone_name=root_timezone_name,
        internal_timezone=root_timezone,
        display_timezone_name=display_timezone_name,
        display_timezone=display_timezone,
        events=tuple(events),
    )
    LOGGER.info("Loaded %d event definition(s) from %s", len(config.events), path)
    return config


def event_to_dict(event: ConfiguredEvent) -> dict[str, Any]:
    """Serialize one validated event into the stable JSON schema."""
    return {
        "id": event.id,
        "name": event.name,
        "region": event.region,
        "source": event.source,
        "verification_status": event.verification_status,
        "timezone": event.timezone_name,
        "messages": {
            "reminder": event.messages.reminder,
            "start": event.messages.start,
        },
        "enabled": event.enabled,
        "placeholder": event.placeholder,
        "schedule": {
            "type": "weekly",
            "weekdays": list(event.schedule.weekdays),
            "times": [value.strftime("%H:%M") for value in event.schedule.times],
            "reminders_minutes": list(event.schedule.reminders_minutes),
        },
    }


def build_event(
    *,
    event_id: str,
    name: str,
    region: str,
    timezone_name: str,
    weekdays: list[str],
    times: list[str],
    reminders_minutes: list[int],
    enabled: bool,
    announcement: str | None = None,
    start_message: str | None = None,
    reminder_message: str | None = None,
    source: str = "Configured by administrator",
    verification_status: str = "admin-configured",
) -> ConfiguredEvent:
    """Build one event from command/API input using the same validators as JSON."""
    if not isinstance(enabled, bool):
        raise EventConfigError("enabled must be a boolean")
    normalized_id = parse_event_id(event_id)
    normalized_name = _required_string(name, "name", "event")
    normalized_region = _required_string(region, "region", "event", max_length=80)
    normalized_timezone_name, timezone = parse_timezone(timezone_name, "event")
    normalized_start = start_message if start_message is not None else announcement
    messages = EventMessages(
        reminder=_message_template(
            reminder_message,
            "messages.reminder",
            "Attention Daevas! {name} starts in {minutes} minutes. Get ready!",
            normalized_name,
        ),
        start=_message_template(
            normalized_start,
            "messages.start",
            "Attention Daevas! {name} has started! Good luck!",
            normalized_name,
        ),
    )
    schedule = parse_schedule(
        {
            "type": "weekly",
            "weekdays": weekdays,
            "times": times,
            "reminders_minutes": reminders_minutes,
        },
        "event",
        enabled=enabled,
    )
    return ConfiguredEvent(
        id=normalized_id,
        name=normalized_name,
        region=normalized_region,
        source=_required_string(source, "source", "event", max_length=300),
        verification_status=_required_string(
            verification_status, "verification_status", "event", max_length=80
        ),
        timezone_name=normalized_timezone_name,
        timezone=timezone,
        messages=messages,
        enabled=enabled,
        placeholder=False,
        schedule=schedule,
    )


def config_to_dict(config: EventConfig) -> dict[str, Any]:
    """Serialize a complete validated configuration."""
    return {
        "version": config.version,
        "timezone": config.internal_timezone_name,
        "display_timezone": config.display_timezone_name,
        "events": [event_to_dict(event) for event in config.events],
    }


def save_event_config_atomic(path: Path, config: EventConfig) -> None:
    """Persist configuration with a same-directory temporary file and replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(config_to_dict(config), temporary, indent=2, ensure_ascii=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(path)
        LOGGER.info("Persisted event configuration atomically to %s", path)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise EventConfigError(f"Could not persist event configuration {path}: {exc}") from exc
