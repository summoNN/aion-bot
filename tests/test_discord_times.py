from pathlib import Path

from bot.cogs.events import _discord_full_time_tag, _discord_time_tags, _next_start_job, _schedule_time_tags
from bot.services.event_sources import load_event_config


def test_discord_time_tags_use_relative_and_localized_formats() -> None:
    rendered = _discord_time_tags("2026-10-16T12:00:00+00:00")

    assert rendered == "<t:1792152000:R> · <t:1792152000:F>"


def test_next_start_job_ignores_reminders_before_start() -> None:
    jobs = [
        {"event_id": "rift-time", "reminder_minutes": 5, "next_run": "2026-10-16T11:55:00+00:00"},
        {"event_id": "rift-time", "reminder_minutes": 0, "next_run": "2026-10-16T12:00:00+00:00"},
    ]

    assert _next_start_job(jobs, "rift-time") == jobs[1]


def test_events_command_uses_localized_discord_time_tags() -> None:
    config = load_event_config(Path("config/events.json"))
    event = config.by_id("rift-time")
    assert event is not None
    job = {"event_id": event.id, "reminder_minutes": 0, "next_run": "2026-10-16T12:00:00+00:00"}

    assert _discord_full_time_tag(job["next_run"]) == "<t:1792152000:F>"
    schedule = _schedule_time_tags(event, job)
    assert schedule.startswith("<t:1792108800:t>")
    assert schedule.count(":t>") == 8
