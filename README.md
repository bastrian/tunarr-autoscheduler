# Tunarr AutoScheduler

Tunarr AutoScheduler is a FastAPI-based scheduling companion for
[Tunarr](https://github.com/chrisbenincasa/tunarr). It reads media metadata from
Jellyfin, builds validated channel schedules, and uploads approved programming
back to Tunarr.

The project is designed for Docker-first deployments, but it can also be run as
a normal Python application for development.

## Features

- Authenticated admin UI for channels, playlists, schedules, jobs, settings, and
  health checks.
- First-run setup flow for admin credentials, Jellyfin, Tunarr, and timezone.
- Read-only Jellyfin media sync for episodes and movies.
- Scheduler-owned playlists with categories, tags, channel scope, and media
  filters.
- Daypart scheduling with off-air loops, custom show lists, playlist priority,
  variable movie blocks, continuity clips, and ad planning.
- Schedule versioning with preview, approval, upload, diff, rollback,
  bulk-delete, cleanup, and upload history.
- Public EPG with configurable access: disabled, Jellyfin login, or public,
  including day, evening, week, compact 7-day, JSON, XMLTV, and standalone
  export views.
- Recommendation tools that can build playlists, dayparts, or channel plans from
  Jellyfin metadata, optional external metadata, and optional Jellystat signals.
- Notifications via Telegram, email, and webhook with event-specific routing.
- Backups, diagnostic bundles, schedule health checks, and CLI commands for
  operational tasks.
- Audit log for admin actions, uploads, schedule lifecycle changes, and
  settings updates with sensitive values redacted.

## Requirements

- Python 3.12+
- Docker and Docker Compose for containerized use
- Tunarr
- Jellyfin

SQLite is the current database backend. The default runtime database lives under
`~/.tunarr/scheduler.db`.

## Quick Start With Docker Compose

Create a `.env` file from the example and adjust it for your deployment:

```bash
cp .env.example .env
```

Build and start the scheduler:

```bash
docker compose up -d --build scheduler
```

Open the web UI and complete the setup wizard. The app creates
`~/.tunarr/config.yaml` on first start and stays in setup mode until admin auth,
Jellyfin credentials, and the Tunarr URL are configured.

## Docker Image Build

The repository can build a production image directly:

```bash
docker build --target runtime -t tunarr-autoscheduler:latest .
```

GitHub Actions builds the runtime image on every push to `main`, every
`v*` tag, and manual workflow runs. Published images are available from GitHub
Container Registry:

```bash
docker pull ghcr.io/bastrian/tunarr-autoscheduler:latest
docker pull ghcr.io/bastrian/tunarr-autoscheduler:main
docker pull ghcr.io/bastrian/tunarr-autoscheduler:v0.1.0
```

Use a version tag for production deployments once releases are cut. The
`latest` tag follows the default branch and is best treated as a moving build.

For test/lint/typecheck jobs:

```bash
docker compose --profile test build scheduler-test
docker compose --profile test run --rm scheduler-test
docker compose --profile test run --rm scheduler-test python -m ruff check tunarr_autoscheduler tests
docker compose --profile test run --rm scheduler-test python -m mypy tunarr_autoscheduler
```

## Docker Swarm

`docker-stack.yml` provides a Swarm-friendly service definition. Set the values
in `.env` or your deployment environment before deploying:

```bash
docker stack deploy -c docker-stack.yml tunarr-autoscheduler
```

The stack file expects an existing external Docker network. Configure it with
`SCHEDULER_NETWORK`.

## Local Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
python -m tunarr_autoscheduler.main
```

Useful checks:

```bash
python -m pytest tests/
python -m ruff check tunarr_autoscheduler tests
python -m mypy tunarr_autoscheduler
```

## CLI

Run commands either locally or inside the scheduler container:

```bash
python -m tunarr_autoscheduler.main sync-channels
python -m tunarr_autoscheduler.main list-schedules CHANNEL_ID
python -m tunarr_autoscheduler.main generate-schedule CHANNEL_ID
python -m tunarr_autoscheduler.main generate-schedule CHANNEL_ID --mode follow-up
python -m tunarr_autoscheduler.main upload-schedule CHANNEL_ID VERSION
python -m tunarr_autoscheduler.main schedule-health
python -m tunarr_autoscheduler.main backup-data
python -m tunarr_autoscheduler.main diagnostic-bundle
```

Schedule generation supports:

- `fresh`: plan from the configured timezone/day boundary.
- `follow-up`: attach after the latest valid planned end and reserve already
  planned episodes/movies.

Upload diagnostics:

```bash
python -m tunarr_autoscheduler.main upload-schedule CHANNEL_ID VERSION --dry-run
python -m tunarr_autoscheduler.main upload-schedule CHANNEL_ID VERSION --dump-payload
python -m tunarr_autoscheduler.main upload-schedule CHANNEL_ID VERSION --dump-tunarr-payload
python -m tunarr_autoscheduler.main upload-schedule CHANNEL_ID VERSION --dump-generated
python -m tunarr_autoscheduler.main upload-schedule CHANNEL_ID VERSION --time-compat
```

## Configuration

Runtime config is stored in:

```text
~/.tunarr/config.yaml
```

Inside the Docker image this maps to:

```text
/root/.tunarr/config.yaml
```

Important sections:

- `auth`: admin username, password hash, and session secret.
- `jellyfin`: read-only Jellyfin URL, API key, user ID, and sync interval.
- `tunarr`: Tunarr URL.
- `channels`: scheduler-owned channel configuration.
- `metadata`: optional TMDB, TVDB, OMDb, and Jellystat integrations.
- `notifications`: Telegram, email, webhook, and routing rules.
- `public_access`: public EPG access mode.
- `backups`: automatic backup settings.

Do not commit a real `config.yaml`, database, logs, backups, or API keys.

## Public EPG Access

The public EPG can be configured in Settings:

- `disabled`: public EPG routes return 404.
- `jellyfin_login`: users must authenticate against Jellyfin.
- `public`: public EPG routes are openly readable.

The public EPG is read-only and intentionally separated from the admin UI.

Use `compact=1` with the week view for a condensed next-7-days guide:

```text
/epg?view=week&compact=1
/public/epg.json?view=week&compact=1
```

## Audit Log

The authenticated admin UI includes `/audit` for operational traceability.
It records schedule generation, approval, upload, rollback, cleanup, deletion,
channel changes, and settings updates. Secrets and API tokens are redacted
before details are persisted.

## Security Notes

- Jellyfin access is read-only.
- Admin auth is session-cookie based and configured during setup.
- Public EPG access should be chosen deliberately before exposing the service.
- Use HTTPS and a reverse proxy in production.
- Keep secrets in runtime config or environment variables, not in the
  repository.
- Rotate credentials if they were ever committed or shared outside your
  deployment.

## Repository Hygiene

The production repository should contain source code, tests, Docker files, and
generic operational docs only. Keep private keys, server-specific runbooks,
local `.venv` folders, caches, generated databases, logs, and ad-hoc handoff
notes outside the repository.
