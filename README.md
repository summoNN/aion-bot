# aion2-event-announcer

Production-oriented Discord bot for announcing AION 2 events in configured Discord voice channels. It uses Python 3.12+, `discord.py` voice support, APScheduler, `python-dotenv`, `edge-tts`, and FFmpeg.

The checked-in European schedule contains community observations for the AION 2 Global International Release. They are explicitly marked **unverified** and are not official game data. Administrators can edit, disable, or replace them.

## Current schedule

Schedules are calculated internally in UTC. `/next` and event listings show the configured display timezone, `Europe/Rome`; the display automatically reflects European daylight-saving changes.

- **Spacetime Rift**: every 3 hours at `00:00`, `03:00`, `06:00`, `09:00`, `12:00`, `15:00`, `18:00`, and `21:00` UTC.
- **Shugo Festival**: every hour at minute `00` UTC.
- Reminders: 5 minutes before and at the configured start time.

The source and verification status are shown by `/events`. Configure `source` and `verification_status` in `config/events.json` when replacing the community observations.

## Windows 11 setup

1. Install Python 3.12 or newer from [python.org](https://www.python.org/downloads/windows/) and select **Add python.exe to PATH**.

2. In PowerShell, create and activate a virtual environment:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   ```

   If activation is blocked, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

3. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`. Set `GUILD_ID` to a test server ID during development so slash commands sync quickly. Never print or commit the token.

4. Start the bot:

   ```powershell
   python main.py
   ```

## Discord Developer Portal and permissions

In **Developer Portal → Application → Bot**, copy the token to `.env`. No privileged gateway intent is required. In **OAuth2 → URL Generator**, select `bot` and `applications.commands`.

Grant the bot these permissions in the invite URL or channel overrides:

- View Channels
- Connect
- Speak

The bot must also be able to send messages and use application commands in the command channel. `/setchannel`, `/setvoicechannel`, `/autojoin`, `/toggleevent`, `/setevent`, `/removeevent`, `/reloadschedule`, `/join`, `/leave`, `/testvoice`, `/testannouncement`, and `/volume` require **Manage Server**. `/voicestatus`, `/events`, `/next`, `/status`, and `/ping` do not.

Commands are guild-scoped when `GUILD_ID` is set and global otherwise.

`/join` intentionally has no channel argument: it connects to the saved channel. `/setchannel channel:<voice channel>` is the only command that accepts a channel; standard Voice and Stage channels are supported. If Discord displays `/join` with a channel option after an update, restart/redeploy the bot and reopen Discord's command menu; the startup sync removes stale global development definitions when `GUILD_ID` is set.

The bot must already be a member of the `GUILD_ID` server for guild-scoped synchronization. A `403 Missing Access` sync message usually means the ID is wrong or the bot was not invited to that server with both `bot` and `applications.commands` scopes. The bot logs the problem, falls back to global synchronization, and continues starting the scheduler and voice service.

## Bot-Hosting.net deployment

Bot-Hosting.net supports Python Discord bots as application deployments. Create an **Application** deployment with the Python runtime, upload this project from GitHub or an archive, and set the panel's **Entry File (`STARTUP_FILE`)** to:

```text
main.py
```

The platform installs Python dependencies from `requirements.txt` during startup. Do not upload `.env`; add these values in the deployment's **Environment** panel instead, marking `DISCORD_TOKEN` as a secret:

```text
DISCORD_TOKEN=your_real_discord_bot_token
GUILD_ID=your_test_guild_id
EVENTS_CONFIG_PATH=config/events.json
DEFAULT_TIMEZONE=UTC
TTS_VOICE=en-US-AriaNeural
TTS_CACHE_DIR=cache/tts
FFMPEG_EXECUTABLE=ffmpeg
VOICE_CONFIG_PATH=config/voice_channels.json
VOICE_VOLUME=1.0
LOG_LEVEL=INFO
HEALTHCHECK_PATH=cache/health/ready
```

After the first start, check the deployment console for the JSON `Logged in` and scheduler messages. Invite the bot with **View Channels**, **Connect**, **Speak**, and application-command permissions, then run `/setchannel` in the target guild. A newly configured channel enables autojoin by default. Use `/voicestatus` to confirm the voice connection and latency.

The deployment must provide an executable named `ffmpeg` for voice playback. If the console reports that FFmpeg cannot be started, configure the host's available FFmpeg path in `FFMPEG_EXECUTABLE` or use the Docker/VPS deployment below; the project cannot install a system executable through Python's `requirements.txt`. The Bot-Hosting deployment is a Python runtime deployment, so the included `Dockerfile` is not used there.

Keep `config/events.json`, `config/voice_channels.json`, and `cache/tts/` on the deployment filesystem. The first two contain the schedule and per-guild voice target; the cache avoids regenerating repeated speech. The included `.bothostingignore` prevents local secrets, tests, caches, and Docker-only files from being uploaded by the Bot-Hosting CLI. If using the free plan, remember that the host currently requires manual renewal every four days.

Useful platform references: [Python hosting](https://bot-hosting.net/python-hosting), [Create a deployment](https://bot-hosting.net/docs/guides/create-a-deployment), and [Set up a deployment](https://bot-hosting.net/docs/guides/set-up-a-deployment).

## Voice setup

Install a trusted Windows FFmpeg build from the [FFmpeg download page](https://ffmpeg.org/download.html), add its `bin` directory to `Path`, open a new PowerShell window, and verify:

```powershell
ffmpeg -version
```

Alternatively set an absolute path in `.env`:

```text
FFMPEG_EXECUTABLE=C:\ffmpeg\bin\ffmpeg.exe
```

Use `/setchannel` (or `/setvoicechannel`) as a Manage Server member to persist a voice target for that guild. A newly configured target enables autojoin by default and the bot connects on startup, even when the channel is empty. `/autojoin enable` and `/autojoin disable` control persistent startup behavior. `/join` connects immediately and enables reconnection for the current process; `/leave` intentionally disconnects and suspends automatic retries until `/join` or `/autojoin enable`. The bot never joins an arbitrary voice channel selected from a user's current presence.

`edge-tts` generates and caches MP3 audio under `TTS_CACHE_DIR`. Each guild has an asynchronous playback queue, so simultaneous announcements do not overlap. Unexpected disconnects retry with exponential backoff and a single per-guild connection lock prevents duplicate voice clients. `/voicestatus` reports the configured channel, connection state, voice latency, and retry state. `/testvoice rift` and `/testvoice shugo` exercise the configured announcement messages; `/volume` controls per-guild playback volume from `0.0` to `2.0`.

The bot deliberately does not generate fake voice activity or play silent audio to keep a channel alive. Discord voice connections remain active because the bot process maintains the real connection; therefore a continuously running host is required.

## 24/7 Docker/VPS deployment

Docker includes FFmpeg and the Discord voice dependencies. Copy the environment template, set the real token, configure at least one guild's voice channel with `/setchannel`, and start the service:

```bash
cp .env.example .env
# edit .env and set DISCORD_TOKEN (and optionally GUILD_ID)
mkdir -p cache config
docker compose up -d --build
docker compose logs -f
```

`docker-compose.yml` uses `restart: unless-stopped` and mounts `config/` and `cache/` so guild voice settings, schedules, and TTS files survive container recreation. The image health check requires a recent bot-ready heartbeat; Docker marks the container unhealthy if the process has not reached Discord or resumed for two minutes. A VPS, server, or other continuously running host is required for 24/7 announcements. If the host is powered off or loses its network, no bot can maintain a voice connection during that outage; the service reconnects after the host and Discord connection recover.

For a Linux VPS using bind mounts, ensure the container user can write the mounted directories, for example `sudo chown -R 10001:10001 config cache`, or use Docker-managed volumes instead. Never put the token in the image or compose file; `.env` is excluded from the image.

## Configuration

`.env` options:

```text
DISCORD_TOKEN=
GUILD_ID=
EVENTS_CONFIG_PATH=config/events.json
DEFAULT_TIMEZONE=UTC
TTS_VOICE=en-US-AriaNeural
TTS_CACHE_DIR=cache/tts
FFMPEG_EXECUTABLE=ffmpeg
VOICE_CONFIG_PATH=config/voice_channels.json
VOICE_VOLUME=1.0
LOG_LEVEL=INFO
HEALTHCHECK_PATH=cache/health/ready
```

`config/events.json` is validated on startup and by `/reloadschedule`. Each event has a stable ID, name, region, IANA timezone, `source`, `verification_status`, enabled state, messages, weekdays, daily `HH:MM` times, and reminder offsets. All times must be timezone-aware through the event timezone and are internally scheduled by APScheduler without naive datetimes. Midnight reminders, daylight-saving transitions, restart recovery, deterministic job IDs, and duplicate delivery prevention are handled by the scheduler. Expired downtime announcements are not replayed.

Example event shape:

```json
{
  "id": "rift-time",
  "name": "Spacetime Rift",
  "region": "European servers",
  "timezone": "UTC",
  "source": "Community observations; not officially verified",
  "verification_status": "unverified community report",
  "messages": {
    "reminder": "Attention Daevas! Rift Time begins in five minutes!",
    "start": "Rift Time is now open! You have ten minutes to enter!"
  },
  "enabled": true,
  "placeholder": false,
  "schedule": {
    "type": "weekly",
    "weekdays": ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
    "times": ["00:00", "03:00"],
    "reminders_minutes": [5, 0]
  }
}
```

Use `/toggleevent` to enable or disable Rift and Shugo independently. `/setevent` can create or replace any validated schedule, while `/removeevent` deletes one. All schedule writes use an atomic same-directory replacement. `config/voice_channels.json` stores per-guild channel IDs and autojoin state using the same restart-safe approach.

## Testing

Install test dependencies and run the focused tests:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```

The tests cover UTC timing and midnight rollover, queue serialization, and scheduler duplicate prevention. Live Discord, edge-tts, and FFmpeg connectivity must be tested in a real configured environment.

## Project structure

```text
.
├── main.py
├── bot/
│   ├── cogs/events.py
│   └── services/
│       ├── event_sources.py
│       ├── scheduler.py
│       ├── tts.py
│       ├── voice.py
│       └── voice_config.py
├── config/events.json
├── config/voice_channels.json
├── requirements.txt
├── requirements-dev.txt
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .bothostingignore
├── .env.example
├── .gitignore
└── README.md
```
