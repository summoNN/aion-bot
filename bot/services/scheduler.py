"""APScheduler integration for timezone-aware weekly event reminders."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.services.event_sources import (
    ConfiguredEvent,
    EventConfig,
    EventConfigError,
    load_event_config,
    save_event_config_atomic,
)

LOGGER = logging.getLogger(__name__)
EventCallback = Callable[[ConfiguredEvent, int, datetime], Awaitable[None]]
MISFIRE_GRACE_SECONDS = 30
DST_FOLD_WINDOW_SECONDS = 3700


class EventScheduler:
    """Load, persist, and schedule event occurrences without replaying old jobs."""

    def __init__(self, config_path: Path, default_timezone: str, callback: EventCallback) -> None:
        self._config_path = config_path
        self._default_timezone = default_timezone
        self._callback = callback
        self._scheduler: AsyncIOScheduler | None = None
        self._config: EventConfig | None = None
        self._mutation_lock = asyncio.Lock()
        self._dedupe_lock = asyncio.Lock()
        self._delivered: set[str] = set()
        self._inflight: set[str] = set()

    @property
    def config(self) -> EventConfig | None:
        return self._config

    @property
    def running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    async def start(self) -> None:
        if self.running:
            return
        await self.reload()
        if self._scheduler is not None:
            self._scheduler.start()
            LOGGER.info("Event scheduler started with %d job(s)", len(self._scheduler.get_jobs()))

    async def stop(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            LOGGER.info("Event scheduler stopped")
        self._scheduler = None

    async def reload(self) -> None:
        """Reload validated configuration and replace jobs without duplicates."""
        async with self._mutation_lock:
            new_config = load_event_config(self._config_path, self._default_timezone)
            was_running = self.running
            if self._scheduler is not None and was_running:
                self._scheduler.shutdown(wait=False)
                self._scheduler = None
            if self._scheduler is None:
                self._scheduler = AsyncIOScheduler(timezone=None)
            self._scheduler.remove_all_jobs()
            self._config = new_config
            self._scheduler.configure(timezone=_scheduler_timezone(new_config))
            self._add_jobs(new_config)
            if was_running:
                self._scheduler.start()
            LOGGER.info("Event scheduler reloaded with %d job(s)", len(self._scheduler.get_jobs()))

    async def upsert_event(self, event: ConfiguredEvent) -> None:
        """Validate at the model boundary, atomically persist, and reload an event."""
        async with self._mutation_lock:
            current = self._config or load_event_config(self._config_path, self._default_timezone)
            events = tuple(existing for existing in current.events if existing.id != event.id) + (event,)
            save_event_config_atomic(
                self._config_path,
                EventConfig(
                    current.version,
                    current.internal_timezone_name,
                    current.internal_timezone,
                    current.display_timezone_name,
                    current.display_timezone,
                    events,
                ),
            )
            await self._reload_unlocked()

    async def remove_event(self, event_id: str) -> None:
        """Atomically remove one event and reload the scheduler."""
        async with self._mutation_lock:
            current = self._config or load_event_config(self._config_path, self._default_timezone)
            if current.by_id(event_id) is None:
                raise EventConfigError(f"Unknown event id: {event_id}")
            events = tuple(event for event in current.events if event.id != event_id.strip().lower())
            save_event_config_atomic(
                self._config_path,
                EventConfig(
                    current.version,
                    current.internal_timezone_name,
                    current.internal_timezone,
                    current.display_timezone_name,
                    current.display_timezone,
                    events,
                ),
            )
            await self._reload_unlocked()

    async def _reload_unlocked(self) -> None:
        new_config = load_event_config(self._config_path, self._default_timezone)
        was_running = self.running
        if self._scheduler is not None and was_running:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        if self._scheduler is None:
            self._scheduler = AsyncIOScheduler(timezone=None)
        self._scheduler.remove_all_jobs()
        self._config = new_config
        self._scheduler.configure(timezone=_scheduler_timezone(new_config))
        self._add_jobs(new_config)
        if was_running:
            self._scheduler.start()

    def _add_jobs(self, config: EventConfig) -> None:
        if self._scheduler is None:
            return
        for event in config.events:
            if not event.enabled:
                LOGGER.info("Skipping disabled event %s", event.id)
                continue
            for weekday in event.schedule.weekdays:
                weekday_index = _weekday_index(weekday)
                for event_time in event.schedule.times:
                    for reminder in event.schedule.reminders_minutes:
                        fire_weekday, fire_time = _reminder_wall_time(weekday_index, event_time, reminder)
                        job_id = _job_id(event.id, weekday_index, event_time, reminder)
                        self._scheduler.add_job(
                            self._run_event,
                            trigger=CronTrigger(
                                day_of_week=fire_weekday,
                                hour=fire_time.hour,
                                minute=fire_time.minute,
                                timezone=event.timezone,
                            ),
                            args=[event.id, weekday_index, event_time.strftime("%H:%M"), reminder],
                            id=job_id,
                            replace_existing=True,
                            coalesce=True,
                            max_instances=1,
                            misfire_grace_time=MISFIRE_GRACE_SECONDS,
                        )

    async def _run_event(
        self,
        event_id: str,
        weekday_index: int,
        event_time_value: str,
        reminder_minutes: int,
    ) -> None:
        config = self._config
        event = config.by_id(event_id) if config is not None else None
        if event is None or not event.enabled:
            return
        event_time = time.fromisoformat(event_time_value)
        now = datetime.now(timezone.utc).astimezone(event.timezone)
        occurrence = _find_occurrence(now, weekday_index, event_time, reminder_minutes, event.timezone)
        if occurrence is None:
            LOGGER.warning("Could not resolve current occurrence for event job %s", event_id)
            return
        fire_time = occurrence - timedelta(minutes=reminder_minutes)
        if abs(now.timestamp() - fire_time.timestamp()) > DST_FOLD_WINDOW_SECONDS:
            LOGGER.info("Skipping expired event reminder %s for %s", event_id, occurrence.isoformat())
            return
        dedupe_key = f"{event.id}:{occurrence.strftime('%Y-%m-%dT%H:%M')}:{reminder_minutes}"
        async with self._dedupe_lock:
            if dedupe_key in self._delivered or dedupe_key in self._inflight:
                LOGGER.debug("Skipping duplicate event reminder %s", dedupe_key)
                return
            self._inflight.add(dedupe_key)
        try:
            await self._callback(event, reminder_minutes, occurrence)
        except Exception:
            LOGGER.exception("Announcement callback failed for event %s", event.id)
        else:
            async with self._dedupe_lock:
                self._delivered.add(dedupe_key)
                self._inflight.discard(dedupe_key)
                if len(self._delivered) > 10000:
                    self._delivered.clear()
        finally:
            async with self._dedupe_lock:
                self._inflight.discard(dedupe_key)

    def job_descriptions(self) -> list[dict[str, Any]]:
        """Return stable status records with timezone-aware next-run values."""
        if self._scheduler is None:
            return []
        descriptions: list[dict[str, Any]] = []
        for job in self._scheduler.get_jobs():
            event_id, weekday, event_time, reminder = job.args
            event = self._config.by_id(event_id) if self._config else None
            next_run = getattr(job, "next_run_time", None)
            descriptions.append(
                {
                    "id": job.id,
                    "event_id": event_id,
                    "event_name": event.name if event else event_id,
                    "region": event.region if event else "unknown",
                    "reminder_minutes": reminder,
                    "next_run": next_run.isoformat() if next_run else None,
                    "next_run_display": (
                        next_run.astimezone(self._config.display_timezone).isoformat()
                        if next_run and self._config
                        else None
                    ),
                    "display_timezone": self._config.display_timezone_name if self._config else "unknown",
                    "timezone": event.timezone_name if event else "unknown",
                    "weekday": weekday,
                    "time": event_time,
                }
            )
        return sorted(
            descriptions,
            key=lambda item: datetime.fromisoformat(item["next_run"]).timestamp()
            if item["next_run"]
            else float("inf"),
        )


def _scheduler_timezone(config: EventConfig) -> Any:
    """Run APScheduler in UTC; event wall-clock schedules are also UTC by default."""
    return config.internal_timezone


def _weekday_index(value: str) -> int:
    return ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday").index(value)


def _reminder_wall_time(weekday_index: int, event_time: time, reminder_minutes: int) -> tuple[int, time]:
    """Calculate the trigger weekday/time, including reminders before midnight."""
    reference = datetime(2024, 1, 1) + timedelta(days=weekday_index)
    event_wall = reference.replace(hour=event_time.hour, minute=event_time.minute)
    fire_wall = event_wall - timedelta(minutes=reminder_minutes)
    return fire_wall.weekday(), fire_wall.time()


def _find_occurrence(
    now: datetime,
    weekday_index: int,
    event_time: time,
    reminder_minutes: int,
    timezone: Any,
) -> datetime | None:
    """Find the aware local occurrence nearest the current scheduler fire."""
    candidates: list[tuple[float, datetime]] = []
    for offset in range(-2, 3):
        candidate_date = now.date() + timedelta(days=offset)
        if candidate_date.weekday() != weekday_index:
            continue
        occurrence = datetime.combine(candidate_date, event_time, tzinfo=timezone)
        fire_time = occurrence - timedelta(minutes=reminder_minutes)
        candidates.append((abs(now.timestamp() - fire_time.timestamp()), occurrence))
    if not candidates:
        return None
    distance, occurrence = min(candidates, key=lambda item: item[0])
    return occurrence if distance <= DST_FOLD_WINDOW_SECONDS else None


def _job_id(event_id: str, weekday_index: int, event_time: time, reminder_minutes: int) -> str:
    return f"event:{event_id}:{weekday_index}:{event_time.strftime('%H%M')}:r{reminder_minutes}"
