from __future__ import annotations

import asyncio
from datetime import datetime, time, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from bot.services.event_sources import load_event_config
from bot.services.scheduler import EventScheduler, _reminder_wall_time


def test_midnight_reminder_rolls_to_previous_weekday() -> None:
    weekday, fire_time = _reminder_wall_time(0, time(0, 0), 5)
    assert weekday == 6
    assert fire_time == time(23, 55)


def test_checked_in_schedule_has_expected_utc_occurrences() -> None:
    config = load_event_config(Path("config/events.json"))
    rift = config.by_id("rift-time")
    shugo = config.by_id("shugo-time")
    assert config.internal_timezone_name == "UTC"
    assert config.display_timezone_name == "Europe/Rome"
    assert rift is not None and len(rift.schedule.times) == 8
    assert shugo is not None and [value.strftime("%H:%M") for value in shugo.schedule.times] == ["00:00"]
    assert rift.source.startswith("Community observations")
    assert rift.verification_status.startswith("unverified")


def test_duplicate_delivery_is_suppressed() -> None:
    delivered: list[str] = []

    async def callback(event, reminder, occurrence) -> None:
        delivered.append(f"{event.id}:{reminder}")

    async def run() -> None:
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "events.json"
            config_path.write_text(Path("config/events.json").read_text(encoding="utf-8"), encoding="utf-8")
            scheduler = EventScheduler(config_path, "UTC", callback)
            scheduler._config = load_event_config(config_path)
            now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            with patch("bot.services.scheduler._find_occurrence", return_value=now):
                await scheduler._run_event("rift-time", now.weekday(), now.strftime("%H:%M"), 0)
                await scheduler._run_event("rift-time", now.weekday(), now.strftime("%H:%M"), 0)
            assert delivered == ["rift-time:0"]

    asyncio.run(run())
