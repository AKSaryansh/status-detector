"""
Status Page Monitor — hybrid event-driven monitor for Atlassian Statuspage-powered sites.

Two modes per page:
  1. Webhook (primary)  — receives real-time HTTP POST pushes from Statuspage.
  2. Adaptive polling (fallback) — exponential backoff when quiet, snaps back to
     fast polling when an incident is detected.

Logging:
  - stdout: JSON Lines (pipeable to Datadog/jq/Splunk)
  - logs/<page>.jsonl: per-page JSON Lines files for history
"""

import asyncio
import json
import os
import signal
import sys
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PAGES = [
    ("OpenAI", "https://status.openai.com"),
    # ("Stripe", "https://status.stripe.com"),
    # ("Twilio", "https://status.twilio.com"),
    # ("Datadog", "https://status.datadoghq.com"),
]

LOG_DIR = "logs"
WEBHOOK_PORT = 8080

# Adaptive polling constants
POLL_INITIAL = 300       # 5 minutes — starting interval
POLL_BACKOFF = 2         # double each quiet cycle
POLL_CAP = 1800          # 30 minutes — max interval
POLL_INCIDENT = 30       # 30 seconds — interval during active incident
POLL_COOLDOWN_CYCLES = 10  # stay fast for 10 cycles (~5 min) after last update


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize(name: str) -> str:
    return name.lower().replace(" ", "_")


def _format_ts(raw: str) -> str:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, AttributeError):
        return raw


def _emit(record: dict, log_path: str) -> None:
    """Write a record to both stdout (JSON) and the per-page log file."""
    line = json.dumps(record, default=str)
    print(line, flush=True)
    with open(log_path, "a") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# Core monitor — used by both webhook and polling paths
# ---------------------------------------------------------------------------

class StatusPageMonitor:
    def __init__(self, name: str, base_url: str):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.log_path = os.path.join(LOG_DIR, f"{_sanitize(name)}.jsonl")

        # Conditional-request state
        self.etag: str | None = None
        self.last_modified: str | None = None

        # Dedup
        self.seen_updates: set[tuple[str, str]] = set()
        self._first_run = True

        # Adaptive polling state
        self._interval = POLL_INITIAL
        self._cooldown_remaining = 0

        # Daily summary tracking
        self._today: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._today_incidents: list[dict] = []  # records collected during the day

    # -- Adaptive polling logic ---------------------------------------------

    def _on_new_updates(self) -> None:
        """Called when new incident updates are found — snap to fast polling."""
        self._interval = POLL_INCIDENT
        self._cooldown_remaining = POLL_COOLDOWN_CYCLES

    def _next_interval(self) -> float:
        """Called after each poll cycle — returns seconds to sleep."""
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            return POLL_INCIDENT

        # No recent activity — back off exponentially
        self._interval = min(self._interval * POLL_BACKOFF, POLL_CAP)
        return self._interval

    # -- HTTP fetch ---------------------------------------------------------

    async def fetch(self, session: aiohttp.ClientSession, endpoint: str) -> dict | None:
        url = f"{self.base_url}{endpoint}"
        headers = {}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.last_modified:
            headers["If-Modified-Since"] = self.last_modified

        async with session.get(url, headers=headers) as resp:
            if resp.status == 304:
                return None
            resp.raise_for_status()
            if "ETag" in resp.headers:
                self.etag = resp.headers["ETag"]
            if "Last-Modified" in resp.headers:
                self.last_modified = resp.headers["Last-Modified"]
            return await resp.json()

    # -- Incident processing (shared by poll + webhook) ---------------------

    def process_incidents(self, incidents: list[dict]) -> None:
        """Diff incidents against seen set; emit new updates."""
        found_new = False

        for incident in incidents:
            inc_id = incident["id"]
            inc_name = incident["name"]
            inc_status = incident["status"]

            for update in incident.get("incident_updates", []):
                upd_id = update["id"]
                key = (inc_id, upd_id)

                if key in self.seen_updates:
                    continue
                self.seen_updates.add(key)

                if self._first_run:
                    continue

                found_new = True
                ts_fmt = _format_ts(update.get("updated_at") or update.get("created_at", ""))
                body = update.get("body", "").strip()
                status = update.get("status", inc_status)

                record = {
                    "timestamp": ts_fmt,
                    "page": self.name,
                    "url": self.base_url,
                    "incident_id": inc_id,
                    "incident": inc_name,
                    "status": status,
                    "detail": body,
                }
                _emit(record, self.log_path)
                self._today_incidents.append(record)

        self._first_run = False

        if found_new:
            self._on_new_updates()

    # -- Polling loop -------------------------------------------------------

    async def run_poll(self, session: aiohttp.ClientSession) -> None:
        _log_stderr(f"  [poll] {self.name} — starting at {self._interval}s interval")
        while True:
            try:
                data = await self.fetch(session, "/api/v2/incidents.json")
                if data is not None:
                    self.process_incidents(data.get("incidents", []))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log_stderr(f"  [poll] {self.name} — error: {exc}")

            sleep_for = self._next_interval()
            _log_stderr(f"  [poll] {self.name} — next check in {sleep_for:.0f}s")
            await asyncio.sleep(sleep_for)


    def emit_daily_summary(self) -> None:
        """Emit an end-of-day summary record and reset for the new day."""
        date = self._today
        incidents = self._today_incidents
        count = len(incidents)

        if count == 0:
            summary = {
                "type": "daily_summary",
                "date": date,
                "page": self.name,
                "url": self.base_url,
                "total_updates": 0,
                "unique_incidents": 0,
                "message": f"No incidents reported for {self.name} on {date}.",
                "incidents": [],
            }
        else:
            # Group by incident_id for a concise breakdown
            by_incident: dict[str, dict] = {}
            for r in incidents:
                iid = r["incident_id"]
                if iid not in by_incident:
                    by_incident[iid] = {
                        "incident_id": iid,
                        "incident": r["incident"],
                        "updates": 0,
                        "last_status": r["status"],
                    }
                by_incident[iid]["updates"] += 1
                by_incident[iid]["last_status"] = r["status"]

            summary = {
                "type": "daily_summary",
                "date": date,
                "page": self.name,
                "url": self.base_url,
                "total_updates": count,
                "unique_incidents": len(by_incident),
                "message": f"{len(by_incident)} incident(s) with {count} update(s) for {self.name} on {date}.",
                "incidents": list(by_incident.values()),
            }

        _emit(summary, self.log_path)

        # Reset for the new day
        self._today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._today_incidents = []


def _log_stderr(msg: str) -> None:
    """Operational messages go to stderr so stdout stays pure JSON."""
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Webhook server — receives POST pushes from Statuspage
# ---------------------------------------------------------------------------

# Maps page name → monitor instance (populated at startup)
_monitors_by_name: dict[str, StatusPageMonitor] = {}


async def _webhook_handler(request: web.Request) -> web.Response:
    """Handle incoming Statuspage webhook POST."""
    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="bad json")

    incident = payload.get("incident")
    if not incident:
        return web.Response(status=200, text="ok (ignored, no incident)")

    page_data = payload.get("page", {})
    page_url = page_data.get("status_url", "")

    # Find matching monitor by URL
    monitor = None
    for m in _monitors_by_name.values():
        if m.base_url in page_url or page_url in m.base_url:
            monitor = m
            break

    if monitor:
        _log_stderr(f"  [webhook] {monitor.name} — received push")
        monitor.process_incidents([incident])
    else:
        _log_stderr(f"  [webhook] unknown page: {page_url}")

    return web.Response(status=200, text="ok")


async def _health_handler(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def start_webhook_server(app: web.Application) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    _log_stderr(f"  [webhook] listening on port {WEBHOOK_PORT}")
    return runner


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)

    # Create monitors
    monitors = [StatusPageMonitor(name, url) for name, url in PAGES]
    for m in monitors:
        _monitors_by_name[m.name] = m

    _log_stderr(f"Monitoring {len(monitors)} status page(s):")

    # Start webhook server
    app = web.Application()
    app.router.add_post("/webhook", _webhook_handler)
    app.router.add_get("/health", _health_handler)
    runner = await start_webhook_server(app)

    # Graceful shutdown
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    # Daily summary scheduler — fires at midnight UTC
    async def daily_summary_loop() -> None:
        while True:
            now = datetime.now(timezone.utc)
            # Seconds until next midnight UTC
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            tomorrow = midnight + timedelta(days=1)
            wait = (tomorrow - now).total_seconds()
            _log_stderr(f"  [summary] next daily summary in {wait / 3600:.1f}h")
            await asyncio.sleep(wait)

            _log_stderr("  [summary] emitting daily summaries...")
            for m in monitors:
                m.emit_daily_summary()

    # Start adaptive polling for all pages (fallback alongside webhooks)
    async with aiohttp.ClientSession() as session:
        tasks = [asyncio.create_task(m.run_poll(session)) for m in monitors]
        tasks.append(asyncio.create_task(daily_summary_loop()))
        await shutdown.wait()

        _log_stderr("\nShutting down...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    await runner.cleanup()
    _log_stderr(f"Logs saved in {LOG_DIR}/")


if __name__ == "__main__":
    asyncio.run(main())
