from bot.cogs.events import _discord_time_tags, _next_start_job


def test_discord_time_tags_use_relative_and_localized_formats() -> None:
    rendered = _discord_time_tags("2026-10-16T12:00:00+00:00")

    assert rendered == "<t:1792152000:R> · <t:1792152000:F>"


def test_next_start_job_ignores_reminders_before_start() -> None:
    jobs = [
        {"event_id": "rift-time", "reminder_minutes": 5, "next_run": "2026-10-16T11:55:00+00:00"},
        {"event_id": "rift-time", "reminder_minutes": 0, "next_run": "2026-10-16T12:00:00+00:00"},
    ]

    assert _next_start_job(jobs, "rift-time") == jobs[1]
