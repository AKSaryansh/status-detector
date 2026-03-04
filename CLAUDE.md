# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Email-based status page incident monitor. Receives status page incident emails via SendGrid Inbound Parse webhooks, parses them into structured records, and serves a live dashboard.

Single-file Python application (`email_monitor.py`) using `aiohttp` as the async web framework.

## Running

```bash
pip install -r requirements.txt
python email_monitor.py          # starts on PORT (default 8081)
PORT=3000 python email_monitor.py  # custom port
```

Deployed via Procfile: `web: python email_monitor.py`

## Architecture

All logic lives in `email_monitor.py`:

- **EmailIncidentParser** — parses email subjects matching `[Page Name] Status - Incident Title` into structured dicts; falls back to sender-based page detection
- **Deduplicator** — suppresses duplicate emails within the same UTC minute bucket
- **PageTracker** — accumulates daily incidents per page, emits JSON summary records at midnight UTC
- **`_emit()`** — writes JSON Lines to stdout and per-page files in `logs/` directory

### HTTP Endpoints

| Route | Method | Purpose |
|---|---|---|
| `/webhook/email` | POST | SendGrid Inbound Parse (multipart/form-data) |
| `/health` | GET | Health check |
| `/logs` | GET | HTML incident dashboard (auto-refreshes 30s) |
| `/api/incidents` | GET | Recent incidents JSON (optional `?page=` filter) |

### Logging Convention

- **stdout**: pure JSON Lines (for log aggregators)
- **stderr**: operational messages (startup, dedup notices, summary scheduling)
- **`logs/<page>.jsonl`**: per-page persistent JSON Lines files
