# Mi Fitness Analytics Bot

Portfolio project: a self-hosted Telegram bot that imports data from Xiaomi Health, turns it into a local analytical warehouse, and presents trends, charts, daily briefings, and an AI-assisted chat.

The project is designed around a simple principle: health data and credentials stay on the operator's machine. The repository contains source code, tests, and an empty configuration template only—never account tokens, Telegram IDs, databases, or real health records.

> This is an engineering project, not a medical device. Its reports and AI responses are informational and must not be used for diagnosis or treatment decisions.

## What it demonstrates

- An incremental Xiaomi Health data importer with QR authentication and idempotent Parquet writes.
- A local analytics pipeline built with Polars and DuckDB: sleep, heart rate, SpO2, stress, activity, trends, anomalies, and recovery signals.
- A multi-user Telegram interface built with aiogram, backed by Celery, Redis, RabbitMQ, and SQLite.
- Chart rendering and compact structured reports for an LLM-powered daily brief and metric-aware chat.
- Testable boundaries around cloud API calls, storage, analytics, bot flows, and AI tools.

## Architecture

```text
Xiaomi Health cloud
        |
   extractor/  -- incremental raw data -->  Parquet (local, ignored)
        |                                         |
        +-------------------------------> analytics/ (Polars + DuckDB)
                                                     |
Telegram <--> bot/ <--> Celery worker <-------------+
                  |                 |
               SQLite             Redis / RabbitMQ
                  |
             optional LLM API
```

## Repository layout

| Directory | Purpose |
| --- | --- |
| `extractor/` | Xiaomi authentication, client, decryption, parsing, and incremental ingestion |
| `analytics/` | Transformations, baselines, anomaly detection, reports, and DuckDB views |
| `bot/` | Telegram application, handlers, keyboards, and UI rendering |
| `worker/` | Celery tasks and schedules |
| `ai/` | LLM client, prompts, structured tools, and safety guardrails |
| `charts/` | Metric visualizations |
| `storage/`, `core/` | Application persistence, configuration, user context, and shared utilities |
| `tests/` | Unit, integration, and synthetic-data tests |

## Quick start

Requirements: Python 3.11+, Docker with Compose (for the full bot stack), and your own Telegram/Xiaomi/LLM credentials where applicable.

```bash
git clone <your-fork-or-repository-url>
cd xiomi_pr
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
```

Edit `.env` locally and provide only your own credentials. Do not commit it. Start the full stack with:

```bash
docker compose up --build
```

For development, run the components directly:

```bash
.venv/bin/python -m bot
.venv/bin/celery -A worker.celery_app worker --loglevel=INFO
.venv/bin/celery -A worker.celery_app beat --loglevel=INFO
```

## CLI workflow

Authenticate interactively, import data, and build local reports:

```bash
.venv/bin/python -m extractor login
.venv/bin/python -m extractor sync --backfill-days 30
.venv/bin/python -m extractor status
.venv/bin/python -m analytics build
.venv/bin/python -m analytics report --stdout
```

All generated files are written below `data/`, which is intentionally ignored by Git. It may contain sensitive health data, session tokens, chat history, and local databases; do not share or upload it.

## Configuration and security

`.env.example` documents the required configuration keys. Keep values such as `BOT_TOKEN`, `ANTHROPIC_API_KEY`, `OWNER_IDS`, proxies, and custom endpoints only in a local `.env` or a managed secret store.

The repository has guardrails for common accidental disclosures:

- `.env`, runtime data, caches, private-key formats, and build artefacts are ignored by Git.
- `.dockerignore` prevents local credentials and health data from being sent to the Docker build context.
- No production credentials, user IDs, health records, or generated screenshots are stored in version control.

If a credential is ever committed or otherwise exposed, revoke and replace it immediately; deleting the file in a later commit is not enough.

## Tests

```bash
.venv/bin/python -m pytest
```

The suite uses fakes and synthetic biometric data. It does not require a Xiaomi account, Telegram bot, or LLM API key.

## License

The project is licensed under [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html). Some Xiaomi-protocol implementation work is derived from GPL-licensed upstream research; see the project metadata for details.

This project is not affiliated with Xiaomi, Mi Fitness, Telegram, Anthropic, or their respective services.
