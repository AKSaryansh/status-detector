"""
Email-Based Status Page Monitor — receives status page incident emails via
SendGrid Inbound Parse webhooks.

How it works:
  Status page incident → email to alerts@yourdomain.com → SendGrid receives
  → SendGrid POSTs multipart/form-data to /webhook/email → parsed here.

Logging:
  - stdout: JSON Lines (pipeable to Datadog/jq/Splunk)
  - logs/<page>.jsonl: per-page JSON Lines files for history
"""

import asyncio
import json
import os
import re
import signal
import sys
from datetime import datetime, timedelta, timezone

from aiohttp import web

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOG_DIR = "logs"
WEBHOOK_PORT = int(os.environ.get("PORT", 8081))
MAX_RECENT = 200

# Subject pattern: [Page Name] Status - Incident Title
_SUBJECT_RE = re.compile(
    r"\[(.+?)\]\s*(Investigating|Identified|Monitoring|Resolved|Update)\s*-\s*(.+)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize(name: str) -> str:
    return name.lower().replace(" ", "_")


_recent_records: list[dict] = []


def _emit(record: dict, log_path: str) -> None:
    """Write a record to stdout, per-page log file, and in-memory store."""
    line = json.dumps(record, default=str)
    print(line, flush=True)
    with open(log_path, "a") as f:
        f.write(line + "\n")
    _recent_records.append(record)
    if len(_recent_records) > MAX_RECENT:
        _recent_records.pop(0)


def _log_stderr(msg: str) -> None:
    """Operational messages go to stderr so stdout stays pure JSON."""
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Email incident parser
# ---------------------------------------------------------------------------

class EmailIncidentParser:
    """Parse status-page email subjects into structured incident records."""

    def parse(self, subject: str, text_body: str, from_addr: str) -> dict | None:
        if not subject:
            return None

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        match = _SUBJECT_RE.search(subject)
        if match:
            page_name = match.group(1).strip()
            status = match.group(2).strip()
            title = match.group(3).strip()
        else:
            # Fallback: use raw subject, derive page from sender
            page_name = self._page_from_sender(from_addr, subject)
            status = "unknown"
            title = subject.strip()

        return {
            "timestamp": now,
            "page": page_name,
            "source": "email",
            "from": from_addr,
            "status": status.lower(),
            "incident": title,
            "detail": (text_body or "").strip(),
        }

    @staticmethod
    def _page_from_sender(from_addr: str, subject: str) -> str:
        """Best-effort page name from the sender address or subject."""
        # Try to extract a name from "Name <email>" format
        angle = from_addr.find("<")
        if angle > 0:
            name = from_addr[:angle].strip()
            if name:
                return name
        # Fall back to the local part of the email
        addr = from_addr.strip("<>").split("@")[0] if "@" in from_addr else "unknown"
        return addr


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class Deduplicator:
    """Track seen (subject, minute-bucket) pairs to suppress duplicate emails."""

    def __init__(self) -> None:
        self._seen: set[tuple[str, str]] = set()

    def is_duplicate(self, subject: str) -> bool:
        # Bucket by minute so near-simultaneous duplicates are caught
        bucket = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        key = (subject, bucket)
        if key in self._seen:
            return True
        self._seen.add(key)
        return False

    def clear(self) -> None:
        self._seen.clear()


# ---------------------------------------------------------------------------
# Daily summary tracking
# ---------------------------------------------------------------------------

class PageTracker:
    """Track daily incidents per page for summary emission."""

    def __init__(self) -> None:
        self._today: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._incidents: dict[str, list[dict]] = {}  # page → records

    def record(self, record: dict) -> None:
        page = record["page"]
        self._incidents.setdefault(page, []).append(record)

    def emit_summaries(self) -> None:
        date = self._today
        pages = set(self._incidents.keys())

        if not pages:
            _log_stderr("  [summary] no incidents today")
        else:
            for page in sorted(pages):
                records = self._incidents[page]
                log_path = os.path.join(LOG_DIR, f"{_sanitize(page)}.jsonl")
                unique = {r["incident"] for r in records}
                summary = {
                    "type": "daily_summary",
                    "date": date,
                    "page": page,
                    "total_updates": len(records),
                    "unique_incidents": len(unique),
                    "message": f"{len(unique)} incident(s) with {len(records)} update(s) for {page} on {date}.",
                    "incidents": [r["incident"] for r in records],
                }
                _emit(summary, log_path)

        # Reset for the new day
        self._today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._incidents.clear()


# ---------------------------------------------------------------------------
# Webhook server — receives SendGrid Inbound Parse POSTs
# ---------------------------------------------------------------------------

_parser = EmailIncidentParser()
_dedup = Deduplicator()
_tracker = PageTracker()


async def _email_webhook_handler(request: web.Request) -> web.Response:
    """Handle incoming SendGrid Inbound Parse POST (multipart/form-data)."""
    try:
        data = await request.post()
    except Exception:
        return web.Response(status=400, text="bad request")

    subject = data.get("subject", "")
    text_body = data.get("text", "")
    from_addr = data.get("from", "")

    if not subject:
        return web.Response(status=200, text="ok (ignored, no subject)")

    # Dedup
    if _dedup.is_duplicate(subject):
        _log_stderr(f"  [email] duplicate suppressed: {subject}")
        return web.Response(status=200, text="ok (duplicate)")

    record = _parser.parse(subject, text_body, from_addr)
    if record is None:
        return web.Response(status=200, text="ok (unparseable)")

    # Emit
    log_path = os.path.join(LOG_DIR, f"{_sanitize(record['page'])}.jsonl")
    _emit(record, log_path)
    _tracker.record(record)

    _log_stderr(f"  [email] {record['page']} — {record['status']}: {record['incident']}")
    return web.Response(status=200, text="ok")


async def _health_handler(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _api_incidents_handler(request: web.Request) -> web.Response:
    """Return recent incident records as JSON."""
    page_filter = request.query.get("page", "").strip().lower()
    records = list(reversed(_recent_records))
    if page_filter:
        records = [r for r in records if r.get("page", "").lower() == page_filter]
    return web.json_response(records)


_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Incident Dashboard</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0f1117; color: #c9d1d9; padding: 2rem;
  }
  h1 { font-size: 1.5rem; margin-bottom: 1rem; color: #e6edf3; }
  .meta { font-size: .85rem; color: #8b949e; margin-bottom: 1.5rem; }
  .filters { margin-bottom: 1rem; display: flex; gap: .5rem; flex-wrap: wrap; }
  .filters button {
    background: #21262d; border: 1px solid #30363d; color: #c9d1d9;
    padding: .35rem .75rem; border-radius: 6px; cursor: pointer; font-size: .8rem;
  }
  .filters button:hover, .filters button.active {
    background: #30363d; border-color: #58a6ff; color: #58a6ff;
  }
  table { width: 100%; border-collapse: collapse; font-size: .85rem; }
  th {
    text-align: left; padding: .6rem .75rem; border-bottom: 1px solid #30363d;
    color: #8b949e; font-weight: 600;
  }
  td { padding: .6rem .75rem; border-bottom: 1px solid #21262d; }
  tr:hover td { background: #161b22; }
  .badge {
    display: inline-block; padding: .15rem .5rem; border-radius: 10px;
    font-size: .75rem; font-weight: 600; text-transform: capitalize;
  }
  .badge-investigating { background: #da3633; color: #fff; }
  .badge-identified   { background: #d29922; color: #fff; }
  .badge-monitoring   { background: #58a6ff; color: #fff; }
  .badge-resolved     { background: #238636; color: #fff; }
  .badge-unknown      { background: #484f58; color: #c9d1d9; }
  .detail { max-width: 320px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .empty { text-align: center; padding: 3rem; color: #484f58; }
</style>
</head>
<body>
<h1>Incident Dashboard</h1>
<p class="meta">Auto-refreshes every 30 s &middot; showing last {max_recent} records</p>
<div class="filters" id="filters"></div>
<table>
<thead><tr><th>Time</th><th>Page</th><th>Status</th><th>Incident</th><th>Detail</th></tr></thead>
<tbody id="tbody"><tr><td colspan="5" class="empty">Loading&hellip;</td></tr></tbody>
</table>
<script>
let activeFilter = '';
function statusClass(s) {
  const m = {investigating:'investigating',identified:'identified',monitoring:'monitoring',resolved:'resolved'};
  return m[s] || 'unknown';
}
async function load() {
  const url = activeFilter ? '/api/incidents?page='+encodeURIComponent(activeFilter) : '/api/incidents';
  const res = await fetch(url);
  const data = await res.json();
  const tbody = document.getElementById('tbody');
  if (!data.length) { tbody.innerHTML='<tr><td colspan="5" class="empty">No incidents recorded yet.</td></tr>'; return; }
  // pages for filter buttons
  const pages = [...new Set(data.map(r=>r.page))].sort();
  const fDiv = document.getElementById('filters');
  fDiv.innerHTML = '<button class="'+(activeFilter?'':'active')+'" onclick="setFilter(\\'\\')">All</button>'
    + pages.map(p=>'<button class="'+(activeFilter===p?'active':'')+'" onclick="setFilter(\\''+p.replace(/'/g,"\\\\'")+'\\'\\')">'+p+'</button>').join('');
  tbody.innerHTML = data.map(r =>
    '<tr><td>'+r.timestamp+'</td><td>'+r.page+'</td>'
    +'<td><span class="badge badge-'+statusClass(r.status)+'">'+r.status+'</span></td>'
    +'<td>'+r.incident+'</td>'
    +'<td class="detail" title="'+(r.detail||'').replace(/"/g,'&quot;')+'">'+(r.detail||'—')+'</td></tr>'
  ).join('');
}
function setFilter(p) { activeFilter = p; load(); }
load();
</script>
</body>
</html>
"""


async def _dashboard_handler(_request: web.Request) -> web.Response:
    html = _DASHBOARD_HTML.replace("{max_recent}", str(MAX_RECENT))
    return web.Response(text=html, content_type="text/html")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)

    app = web.Application()
    app.router.add_post("/webhook/email", _email_webhook_handler)
    app.router.add_get("/health", _health_handler)
    app.router.add_get("/logs", _dashboard_handler)
    app.router.add_get("/api/incidents", _api_incidents_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    _log_stderr(f"Email monitor listening on port {WEBHOOK_PORT}")
    _log_stderr("  POST /webhook/email  — SendGrid Inbound Parse endpoint")
    _log_stderr("  GET  /health         — health check")
    _log_stderr("  GET  /logs           — incident dashboard")
    _log_stderr("  GET  /api/incidents  — incidents JSON API")

    # Graceful shutdown
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    # Daily summary scheduler — fires at midnight UTC
    async def daily_summary_loop() -> None:
        while True:
            now = datetime.now(timezone.utc)
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            tomorrow = midnight + timedelta(days=1)
            wait = (tomorrow - now).total_seconds()
            _log_stderr(f"  [summary] next daily summary in {wait / 3600:.1f}h")
            await asyncio.sleep(wait)

            _log_stderr("  [summary] emitting daily summaries...")
            _tracker.emit_summaries()

    summary_task = asyncio.create_task(daily_summary_loop())
    await shutdown.wait()

    _log_stderr("\nShutting down...")
    summary_task.cancel()
    await asyncio.gather(summary_task, return_exceptions=True)
    await runner.cleanup()
    _log_stderr(f"Logs saved in {LOG_DIR}/")


if __name__ == "__main__":
    asyncio.run(main())
