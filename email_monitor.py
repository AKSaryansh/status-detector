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
<title>INCIDENT MONITOR</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap');
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg-primary: #0a0e14; --bg-secondary: #0d1117; --bg-tertiary: #131921;
    --bg-card: #0f1319; --border: #1b2230; --border-bright: #253040;
    --text-primary: #c5cdd8; --text-secondary: #6b7a8d; --text-dim: #3d4b5c;
    --accent: #00e5ff; --accent-dim: rgba(0,229,255,0.08);
    --red: #ff3b5c; --red-dim: rgba(255,59,92,0.1); --red-glow: rgba(255,59,92,0.3);
    --orange: #ff9f43; --orange-dim: rgba(255,159,67,0.1);
    --blue: #3b82f6; --blue-dim: rgba(59,130,246,0.1);
    --green: #00d68f; --green-dim: rgba(0,214,143,0.1); --green-glow: rgba(0,214,143,0.3);
    --gray: #4a5568; --gray-dim: rgba(74,85,104,0.15);
  }
  body {
    font-family: 'JetBrains Mono', 'SF Mono', 'Fira Code', monospace;
    background: var(--bg-primary); color: var(--text-primary);
    min-height: 100vh; overflow-x: hidden;
  }
  .scanline {
    position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,229,255,0.008) 2px, rgba(0,229,255,0.008) 4px);
    pointer-events: none; z-index: 1000;
  }
  .top-bar {
    background: var(--bg-secondary); border-bottom: 1px solid var(--border);
    padding: .6rem 1.5rem; display: flex; align-items: center; justify-content: space-between;
    font-size: .7rem; color: var(--text-secondary);
  }
  .top-bar-left { display: flex; align-items: center; gap: 1.5rem; }
  .top-bar .sys-label { color: var(--accent); font-weight: 600; letter-spacing: .08em; }
  .pulse-dot {
    width: 6px; height: 6px; border-radius: 50%; background: var(--green);
    display: inline-block; margin-right: .4rem; animation: pulse 2s ease-in-out infinite;
    box-shadow: 0 0 6px var(--green-glow);
  }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .3; } }
  .top-bar .clock { font-variant-numeric: tabular-nums; }

  .container { max-width: 1400px; margin: 0 auto; padding: 1.2rem 1.5rem; }

  .header {
    display: flex; align-items: flex-end; justify-content: space-between;
    margin-bottom: 1.2rem; padding-bottom: 1rem; border-bottom: 1px solid var(--border);
  }
  .header h1 {
    font-size: 1.1rem; font-weight: 700; color: var(--accent);
    letter-spacing: .15em; text-transform: uppercase;
  }
  .header h1 span { color: var(--text-dim); font-weight: 300; }
  .header-meta { font-size: .65rem; color: var(--text-dim); text-align: right; line-height: 1.6; }

  .stats-row {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: .8rem; margin-bottom: 1.2rem;
  }
  .stat-card {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 4px;
    padding: .8rem 1rem; position: relative; overflow: hidden;
  }
  .stat-card::before {
    content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 2px;
  }
  .stat-card.card-total::before { background: var(--accent); }
  .stat-card.card-active::before { background: var(--red); }
  .stat-card.card-resolved::before { background: var(--green); }
  .stat-card.card-sources::before { background: var(--blue); }
  .stat-label {
    font-size: .55rem; color: var(--text-dim); text-transform: uppercase;
    letter-spacing: .12em; margin-bottom: .3rem;
  }
  .stat-value { font-size: 1.5rem; font-weight: 700; }
  .stat-card.card-total .stat-value { color: var(--accent); }
  .stat-card.card-active .stat-value { color: var(--red); }
  .stat-card.card-resolved .stat-value { color: var(--green); }
  .stat-card.card-sources .stat-value { color: var(--blue); }

  .panel {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 4px;
    margin-bottom: 1rem;
  }
  .panel-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: .6rem 1rem; border-bottom: 1px solid var(--border);
    font-size: .65rem; color: var(--text-secondary); text-transform: uppercase;
    letter-spacing: .1em;
  }
  .panel-header .label { display: flex; align-items: center; gap: .5rem; }
  .panel-header .corner { color: var(--text-dim); }

  .filters {
    display: flex; gap: .4rem; flex-wrap: wrap; padding: .6rem 1rem;
    border-bottom: 1px solid var(--border); background: rgba(13,17,23,0.5);
  }
  .filters button {
    background: transparent; border: 1px solid var(--border); color: var(--text-secondary);
    padding: .25rem .7rem; border-radius: 2px; cursor: pointer;
    font-family: inherit; font-size: .65rem; letter-spacing: .05em;
    text-transform: uppercase; transition: all .15s;
  }
  .filters button:hover { border-color: var(--accent); color: var(--accent); }
  .filters button.active {
    background: var(--accent-dim); border-color: var(--accent);
    color: var(--accent); font-weight: 600;
  }

  table { width: 100%; border-collapse: collapse; font-size: .72rem; }
  thead th {
    text-align: left; padding: .5rem 1rem; color: var(--text-dim);
    font-weight: 500; font-size: .6rem; text-transform: uppercase;
    letter-spacing: .1em; border-bottom: 1px solid var(--border);
    background: var(--bg-secondary);
  }
  tbody td {
    padding: .55rem 1rem; border-bottom: 1px solid var(--border);
    vertical-align: top; transition: background .1s;
  }
  tbody tr:hover td { background: rgba(0,229,255,0.02); }
  tbody tr { position: relative; }

  .ts { color: var(--text-secondary); font-variant-numeric: tabular-nums; white-space: nowrap; font-size: .65rem; }
  .page-tag {
    display: inline-block; padding: .15rem .5rem; border-radius: 2px;
    font-size: .6rem; font-weight: 500; letter-spacing: .05em;
    background: var(--bg-secondary); border: 1px solid var(--border); color: var(--text-primary);
  }
  .badge {
    display: inline-flex; align-items: center; gap: .35rem;
    padding: .2rem .55rem; border-radius: 2px;
    font-size: .6rem; font-weight: 600; text-transform: uppercase; letter-spacing: .08em;
  }
  .badge::before {
    content: ''; width: 5px; height: 5px; border-radius: 50%; flex-shrink: 0;
  }
  .badge-investigating { background: var(--red-dim); color: var(--red); }
  .badge-investigating::before { background: var(--red); box-shadow: 0 0 6px var(--red-glow); }
  .badge-identified { background: var(--orange-dim); color: var(--orange); }
  .badge-identified::before { background: var(--orange); }
  .badge-monitoring { background: var(--blue-dim); color: var(--blue); }
  .badge-monitoring::before { background: var(--blue); }
  .badge-resolved { background: var(--green-dim); color: var(--green); }
  .badge-resolved::before { background: var(--green); box-shadow: 0 0 6px var(--green-glow); }
  .badge-unknown { background: var(--gray-dim); color: var(--gray); }
  .badge-unknown::before { background: var(--gray); }

  .incident-title { color: var(--text-primary); font-weight: 500; }
  .detail-cell {
    max-width: 350px; color: var(--text-secondary); font-size: .65rem;
    line-height: 1.5; cursor: default;
  }
  .detail-text {
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .detail-cell:hover .detail-text { -webkit-line-clamp: unset; }

  .empty-state {
    text-align: center; padding: 4rem 2rem; color: var(--text-dim);
    font-size: .75rem; letter-spacing: .05em;
  }
  .empty-state .icon { font-size: 1.5rem; margin-bottom: .8rem; opacity: .3; }

  .refresh-bar {
    height: 2px; background: var(--bg-tertiary); margin-top: .5rem; border-radius: 1px; overflow: hidden;
  }
  .refresh-bar .progress {
    height: 100%; width: 0%; background: var(--accent); opacity: .4;
    transition: width 1s linear;
  }

  @media (max-width: 768px) {
    .stats-row { grid-template-columns: repeat(2, 1fr); }
    .container { padding: .8rem; }
    table { font-size: .65rem; }
    .detail-cell { display: none; }
  }
</style>
</head>
<body>
<div class="scanline"></div>
<div class="top-bar">
  <div class="top-bar-left">
    <span class="sys-label">SYS::INCIDENT_MONITOR</span>
    <span><span class="pulse-dot"></span>OPERATIONAL</span>
    <span>WEBHOOK ACTIVE</span>
  </div>
  <span class="clock" id="clock">--:--:-- UTC</span>
</div>
<div class="container">
  <div class="header">
    <h1>INCIDENT FEED <span>// real-time status ingestion</span></h1>
    <div class="header-meta">
      REFRESH INTERVAL: 30s<br>
      BUFFER: {max_recent} RECORDS
    </div>
  </div>
  <div class="stats-row" id="stats">
    <div class="stat-card card-total"><div class="stat-label">Total Events</div><div class="stat-value" id="stat-total">--</div></div>
    <div class="stat-card card-active"><div class="stat-label">Active Incidents</div><div class="stat-value" id="stat-active">--</div></div>
    <div class="stat-card card-resolved"><div class="stat-label">Resolved</div><div class="stat-value" id="stat-resolved">--</div></div>
    <div class="stat-card card-sources"><div class="stat-label">Sources</div><div class="stat-value" id="stat-sources">--</div></div>
  </div>
  <div class="panel">
    <div class="panel-header">
      <span class="label">INCIDENT LOG</span>
      <span class="corner" id="rec-count">0 records</span>
    </div>
    <div class="filters" id="filters"></div>
    <table>
      <thead><tr><th>Timestamp</th><th>Source</th><th>Status</th><th>Incident</th><th>Detail</th></tr></thead>
      <tbody id="tbody"><tr><td colspan="5" class="empty-state"><div class="icon">&gt;_</div>Awaiting incident data&hellip;</td></tr></tbody>
    </table>
  </div>
  <div class="refresh-bar"><div class="progress" id="progress"></div></div>
</div>
<script>
let activeFilter = "";
function statusClass(s) {
  var m = {investigating:"investigating",identified:"identified",monitoring:"monitoring",resolved:"resolved"};
  return m[s] || "unknown";
}
function esc(s) { var d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

// Live clock
function tickClock() {
  var now = new Date();
  document.getElementById("clock").textContent =
    now.toISOString().slice(11,19) + " UTC";
}
setInterval(tickClock, 1000); tickClock();

// Refresh progress bar
var refreshInterval = 30;
var elapsed = 0;
function tickProgress() {
  elapsed++;
  var pct = Math.min((elapsed / refreshInterval) * 100, 100);
  document.getElementById("progress").style.width = pct + "%";
  if (elapsed >= refreshInterval) { elapsed = 0; load(); }
}
setInterval(tickProgress, 1000);

async function load() {
  elapsed = 0;
  document.getElementById("progress").style.width = "0%";

  // Always fetch all data for filters and stats
  var allRes = await fetch("/api/incidents");
  var allData = await allRes.json();
  var allIncidents = allData.filter(function(r){ return !r.type; });
  var pages = Array.from(new Set(allIncidents.map(function(r){return r.page}))).sort();

  // Apply filter for display
  var data = allData;
  if (activeFilter) {
    data = allData.filter(function(r){ return (r.page||"").toLowerCase() === activeFilter.toLowerCase(); });
  }
  var tbody = document.getElementById("tbody");

  // Stats (from displayed data)
  var incidents = data.filter(function(r){ return !r.type; });
  var active = incidents.filter(function(r){ return r.status !== "resolved"; }).length;
  var resolved = incidents.filter(function(r){ return r.status === "resolved"; }).length;
  document.getElementById("stat-total").textContent = incidents.length;
  document.getElementById("stat-active").textContent = active;
  document.getElementById("stat-resolved").textContent = resolved;
  document.getElementById("stat-sources").textContent = pages.length;
  document.getElementById("rec-count").textContent = data.length + " records";

  if (!allData.length) {
    tbody.innerHTML = "<tr><td colspan='5' class='empty-state'><div class='icon'>&gt;_</div>No incidents recorded yet.</td></tr>";
    return;
  }

  // Filters (always built from all data)
  var fDiv = document.getElementById("filters");
  fDiv.innerHTML = "";
  var allBtn = document.createElement("button");
  allBtn.textContent = "ALL";
  if (!activeFilter) allBtn.className = "active";
  allBtn.addEventListener("click", function(){ activeFilter = ""; load(); });
  fDiv.appendChild(allBtn);
  pages.forEach(function(p) {
    var btn = document.createElement("button");
    btn.textContent = p;
    if (activeFilter.toLowerCase() === p.toLowerCase()) btn.className = "active";
    btn.addEventListener("click", function(){ activeFilter = p; load(); });
    fDiv.appendChild(btn);
  });

  // Rows
  var html = "";
  data.forEach(function(r) {
    if (r.type === "daily_summary") {
      html += "<tr><td class='ts'>" + esc(r.date) + "</td><td><span class='page-tag'>" + esc(r.page) + "</span></td>"
        + "<td><span class='badge badge-unknown'>summary</span></td>"
        + "<td class='incident-title'>" + esc(r.message) + "</td>"
        + "<td class='detail-cell'><div class='detail-text'>" + esc((r.incidents||[]).join(", ")) + "</div></td></tr>";
    } else {
      html += "<tr><td class='ts'>" + esc(r.timestamp) + "</td><td><span class='page-tag'>" + esc(r.page) + "</span></td>"
        + "<td><span class='badge badge-" + statusClass(r.status) + "'>" + esc(r.status) + "</span></td>"
        + "<td class='incident-title'>" + esc(r.incident) + "</td>"
        + "<td class='detail-cell'><div class='detail-text'>" + esc(r.detail||"") + "</div></td></tr>";
    }
  });
  tbody.innerHTML = html;
}
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
