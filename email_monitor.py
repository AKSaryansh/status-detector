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

import aiohttp
from aiohttp import web

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOG_DIR = "logs"
WEBHOOK_PORT = int(os.environ.get("PORT", 8081))
MAX_RECENT = 200
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")  # optional server fallback

# Subject pattern: [Page Name] Status - Incident Title
_SUBJECT_RE = re.compile(
    r"\[(.+?)\]\s*(Investigating|Identified|Monitoring|Resolved|Update)\s*-\s*(.+)",
    re.IGNORECASE,
)

# Pattern to detect subscription confirmation emails
_CONFIRM_RE = re.compile(r"confirm your subscription", re.IGNORECASE)
# Extract confirmation URLs from email body
_CONFIRM_URL_RE = re.compile(
    r"https?://[^\s]+/subscriptions/confirm/[A-Za-z0-9_\-]+",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize(name: str) -> str:
    return name.lower().replace(" ", "_")


_recent_records: list[dict] = []
_autoconfirm_log: list[dict] = []


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
# Auto-confirm subscriptions
# ---------------------------------------------------------------------------

async def _auto_confirm(subject: str, text_body: str) -> None:
    """Detect subscription confirmation emails and visit the confirm URL."""
    if not _CONFIRM_RE.search(subject):
        return

    urls = _CONFIRM_URL_RE.findall(text_body or "")
    if not urls:
        _log_stderr(f"  [autoconfirm] confirmation email detected but no URL found: {subject}")
        return

    for url in urls:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    status = resp.status
            entry = {
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "action": "auto_confirmed",
                "subject": subject,
                "url": url,
                "http_status": status,
            }
            _autoconfirm_log.append(entry)
            _log_stderr(f"  [autoconfirm] confirmed: {url} (HTTP {status})")
        except Exception as e:
            entry = {
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "action": "auto_confirm_failed",
                "subject": subject,
                "url": url,
                "error": str(e),
            }
            _autoconfirm_log.append(entry)
            _log_stderr(f"  [autoconfirm] FAILED: {url} — {e}")


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

    # Auto-confirm subscription emails (fire and forget)
    asyncio.create_task(_auto_confirm(subject, text_body))

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


async def _api_autoconfirm_handler(_request: web.Request) -> web.Response:
    """Return auto-confirm log."""
    return web.json_response(list(reversed(_autoconfirm_log)))


# ---------------------------------------------------------------------------
# AI Agent — multi-provider incident query interface
# ---------------------------------------------------------------------------

_AGENT_MODELS = {
    "anthropic": {
        "claude-haiku-4-5-20251001": "Haiku 4.5",
        "claude-sonnet-4-5-20250514": "Sonnet 4.5",
        "claude-opus-4-0-20250514": "Opus 4",
    },
    "openai": {
        "gpt-4o-mini": "GPT-4o Mini",
        "gpt-4o": "GPT-4o",
        "o3-mini": "o3-mini",
    },
    "gemini": {
        "gemini-2.0-flash-lite": "Flash-Lite 2.0",
        "gemini-2.0-flash": "Flash 2.0",
        "gemini-2.5-pro-preview-06-05": "Gemini 2.5 Pro",
    },
    "groq": {
        "llama-3.3-70b-versatile": "Llama 3.3 70B",
        "gemma2-9b-it": "Gemma 2 9B",
        "mixtral-8x7b-32768": "Mixtral 8x7B",
    },
}


def _build_system_prompt() -> str:
    incidents = list(reversed(_recent_records))
    incident_json = json.dumps(incidents[:100], indent=2, default=str)
    return f"""You are an incident analysis agent for a status page monitoring system. You have access to the latest incident data ingested from email notifications from services like GitHub, AWS, Stripe, OpenAI, Vercel, Claude, Datadog, etc.

Your job is to answer questions about these incidents concisely and technically. You can:
- Summarize recent incidents
- Filter by service/page name
- Identify what broke and when
- Track incident progression (investigating → identified → monitoring → resolved)
- Provide timeline analysis
- Highlight active (unresolved) incidents

Be direct, technical, and use short responses. Format with markdown. If the data doesn't contain what the user asks about, say so clearly.

Current incident data ({len(incidents)} records):
```json
{incident_json}
```"""


async def _call_anthropic(api_key: str, model: str, system: str, message: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1024,
                "system": system,
                "messages": [{"role": "user", "content": message}],
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            result = await resp.json()
            if resp.status != 200:
                raise ValueError(result.get("error", {}).get("message", f"Anthropic API error (HTTP {resp.status})"))
            return result["content"][0]["text"]


async def _call_openai(api_key: str, model: str, system: str, message: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1024,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": message},
                ],
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            result = await resp.json()
            if resp.status != 200:
                raise ValueError(result.get("error", {}).get("message", f"OpenAI API error (HTTP {resp.status})"))
            return result["choices"][0]["message"]["content"]


async def _call_groq(api_key: str, model: str, system: str, message: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1024,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": message},
                ],
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            result = await resp.json()
            if resp.status != 200:
                raise ValueError(result.get("error", {}).get("message", f"Groq API error (HTTP {resp.status})"))
            return result["choices"][0]["message"]["content"]


async def _call_gemini(api_key: str, model: str, system: str, message: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers={"Content-Type": "application/json"},
            json={
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"parts": [{"text": message}]}],
                "generationConfig": {"maxOutputTokens": 1024},
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            result = await resp.json()
            if resp.status != 200:
                err = result.get("error", {}).get("message", f"Gemini API error (HTTP {resp.status})")
                raise ValueError(err)
            return result["candidates"][0]["content"]["parts"][0]["text"]


async def _agent_query_handler(request: web.Request) -> web.Response:
    """Handle agent chat queries — proxies to user-selected AI provider."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    user_message = body.get("message", "").strip()
    provider = body.get("provider", "anthropic").strip().lower()
    model = body.get("model", "").strip()
    api_key = body.get("api_key", "").strip()

    if not user_message:
        return web.json_response({"error": "empty message"}, status=400)

    # Validate provider
    if provider not in _AGENT_MODELS:
        return web.json_response({"error": f"unknown provider: {provider}"}, status=400)

    # Validate model belongs to provider
    if model not in _AGENT_MODELS[provider]:
        return web.json_response({"error": f"invalid model for {provider}: {model}"}, status=400)

    # Use provided key, fall back to server env for Anthropic only
    if not api_key and provider == "anthropic" and ANTHROPIC_API_KEY:
        api_key = ANTHROPIC_API_KEY
    if not api_key:
        return web.json_response({"error": "API key required. Your key is never stored — it's used for this request only."}, status=400)

    system = _build_system_prompt()

    try:
        if provider == "anthropic":
            reply = await _call_anthropic(api_key, model, system, user_message)
        elif provider == "openai":
            reply = await _call_openai(api_key, model, system, user_message)
        elif provider == "groq":
            reply = await _call_groq(api_key, model, system, user_message)
        else:
            reply = await _call_gemini(api_key, model, system, user_message)
        return web.json_response({"reply": reply})
    except asyncio.TimeoutError:
        return web.json_response({"error": "API timeout — try again or use a faster model"}, status=504)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=502)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def _agent_models_handler(_request: web.Request) -> web.Response:
    """Return available models per provider."""
    return web.json_response(_AGENT_MODELS)


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

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
    font-size: .7rem; color: var(--text-secondary); overflow: visible; position: relative; z-index: 10;
  }
  .top-bar-left { display: flex; align-items: center; gap: 1.5rem; }
  .top-bar .sys-label { color: var(--accent); font-weight: 600; letter-spacing: .08em; }
  .top-bar-nav { display: flex; gap: .3rem; overflow: visible; }
  .top-bar-nav a {
    color: var(--text-secondary); text-decoration: none; padding: .25rem .6rem;
    border: 1px solid transparent; border-radius: 2px; font-size: .65rem;
    letter-spacing: .05em; text-transform: uppercase; transition: all .15s;
  }
  .top-bar-nav a:hover { border-color: var(--accent); color: var(--accent); }
  .top-bar-nav a.active { border-color: var(--accent); color: var(--accent); background: var(--accent-dim); }
  .top-bar-nav a.agent-link {
    position: relative; color: var(--accent); border-color: var(--accent);
    animation: agentGlow 2.5s ease-in-out infinite;
    overflow: visible;
  }
  /* Glowing border pulse */
  .top-bar-nav a.agent-link::before {
    content: ''; position: absolute; inset: -3px; border-radius: 4px;
    border: 1px solid var(--accent); opacity: 0;
    animation: agentRing 2.5s ease-in-out infinite;
    pointer-events: none;
  }
  /* Bouncing arrow — sits below the button, points up */
  .top-bar-nav a.agent-link .arrow-wrap {
    position: absolute; left: 50%; bottom: -20px;
    transform: translateX(-50%);
    display: flex; flex-direction: column; align-items: center;
    animation: arrowBounce 1.8s ease-in-out infinite;
    pointer-events: none; z-index: 10;
  }
  .top-bar-nav a.agent-link .arrow-wrap svg {
    filter: drop-shadow(0 0 5px rgba(0,229,255,0.7));
  }
  /* Trail particles below arrow */
  .top-bar-nav a.agent-link .arrow-wrap::before,
  .top-bar-nav a.agent-link .arrow-wrap::after {
    content: ''; position: absolute; width: 4px; height: 4px;
    border-radius: 50%; background: var(--accent);
  }
  .top-bar-nav a.agent-link .arrow-wrap::before {
    bottom: -6px; left: calc(50% - 5px);
    animation: particle 1.8s ease-in-out infinite;
  }
  .top-bar-nav a.agent-link .arrow-wrap::after {
    bottom: -4px; left: calc(50% + 3px);
    animation: particle 1.8s ease-in-out infinite .3s;
  }
  @keyframes agentGlow {
    0%,100% { box-shadow: 0 0 4px rgba(0,229,255,0.15); }
    50% { box-shadow: 0 0 14px rgba(0,229,255,0.5), 0 0 28px rgba(0,229,255,0.12); }
  }
  @keyframes agentRing {
    0% { opacity: 0; transform: scale(1); }
    50% { opacity: .6; transform: scale(1.15); }
    100% { opacity: 0; transform: scale(1.3); }
  }
  @keyframes arrowBounce {
    0%,100% { transform: translateX(-50%) translateY(0); }
    50% { transform: translateX(-50%) translateY(4px); }
  }
  @keyframes particle {
    0% { opacity: .8; transform: translateY(0) scale(1); }
    50% { opacity: .3; transform: translateY(10px) scale(0.5); }
    100% { opacity: 0; transform: translateY(16px) scale(0); }
  }
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
  <div class="top-bar-nav">
    <a href="/logs" class="active">DASHBOARD</a>
    <a href="/agent" class="agent-link">AGENT<span class="arrow-wrap"><svg width="14" height="10" viewBox="0 0 14 10" fill="none"><path d="M7 0L13.5 7.5L12 9L7 3.5L2 9L0.5 7.5L7 0Z" fill="var(--accent)"/></svg></span></a>
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

# ---------------------------------------------------------------------------
# Agent HTML
# ---------------------------------------------------------------------------

_AGENT_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>INCIDENT AGENT</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0c0f16; --bg-raised: #141820; --bg-input: #1a1f2b;
    --bg-hover: #1e2535; --surface: #1c2233;
    --border: #2a3245; --border-light: #354055;
    --text: #e2e8f0; --text-secondary: #8994a7; --text-muted: #515d73;
    --accent: #00d4ff; --accent-dim: rgba(0,212,255,0.1); --accent-border: rgba(0,212,255,0.3);
    --anthropic: #d97757; --anthropic-bg: rgba(217,119,87,0.08); --anthropic-border: rgba(217,119,87,0.35);
    --openai: #10a37f; --openai-bg: rgba(16,163,127,0.08); --openai-border: rgba(16,163,127,0.35);
    --gemini: #7b9cf7; --gemini-bg: rgba(123,156,247,0.08); --gemini-border: rgba(123,156,247,0.35);
    --groq: #f55036; --groq-bg: rgba(245,80,54,0.08); --groq-border: rgba(245,80,54,0.35);
    --green: #34d399; --green-glow: rgba(52,211,153,0.3);
    --red: #f87171;
  }
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh; display: flex; flex-direction: column;
  }

  /* --- Top bar --- */
  .top-bar {
    background: var(--bg-raised); border-bottom: 1px solid var(--border);
    padding: .65rem 1.5rem; display: flex; align-items: center; justify-content: space-between;
    font-size: .75rem; color: var(--text-secondary); flex-shrink: 0;
    font-family: 'JetBrains Mono', monospace;
  }
  .top-bar-left { display: flex; align-items: center; gap: 1.2rem; }
  .top-bar .sys-label { color: var(--accent); font-weight: 600; font-size: .7rem; letter-spacing: .06em; }
  .top-bar-nav { display: flex; gap: .3rem; }
  .top-bar-nav a {
    color: var(--text-secondary); text-decoration: none; padding: .3rem .8rem;
    border: 1px solid transparent; border-radius: 6px; font-size: .7rem;
    letter-spacing: .03em; transition: all .15s;
  }
  .top-bar-nav a:hover { border-color: var(--border-light); color: var(--text); background: var(--bg-hover); }
  .top-bar-nav a.active { border-color: var(--accent-border); color: var(--accent); background: var(--accent-dim); }
  .pulse-dot {
    width: 7px; height: 7px; border-radius: 50%; background: var(--green);
    display: inline-block; margin-right: .4rem; animation: pulse 2s ease-in-out infinite;
    box-shadow: 0 0 8px var(--green-glow);
  }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .3; } }
  .clock { font-variant-numeric: tabular-nums; font-family: 'JetBrains Mono', monospace; font-size: .7rem; }

  /* --- Setup panel --- */
  .setup-panel {
    flex-shrink: 0; background: var(--bg-raised);
    border-bottom: 1px solid var(--border); padding: 1.2rem 1.5rem;
  }
  .setup-inner { max-width: 920px; margin: 0 auto; }
  .setup-title {
    font-size: .65rem; font-weight: 600; color: var(--text-muted);
    text-transform: uppercase; letter-spacing: .1em; margin-bottom: 1rem;
    font-family: 'JetBrains Mono', monospace;
  }
  .setup-steps { display: flex; flex-direction: column; gap: 1rem; }
  .setup-row { display: flex; gap: 1rem; align-items: flex-end; }

  .step-label {
    display: flex; align-items: center; gap: .5rem; margin-bottom: .6rem;
  }
  .step-num {
    width: 22px; height: 22px; border-radius: 50%; display: flex; align-items: center;
    justify-content: center; font-size: .6rem; font-weight: 700;
    background: var(--accent-dim); color: var(--accent); border: 1px solid var(--accent-border);
    font-family: 'JetBrains Mono', monospace;
  }
  .step-label span {
    font-size: .75rem; font-weight: 600; color: var(--text);
  }

  /* Provider cards */
  .provider-cards { display: flex; gap: .5rem; flex-wrap: wrap; }
  .provider-card {
    display: flex; align-items: center; gap: .6rem;
    padding: .65rem 1.1rem; border: 2px solid var(--border); border-radius: 10px;
    cursor: pointer; transition: all .2s; background: var(--bg-input);
  }
  .provider-card:hover { border-color: var(--border-light); background: var(--bg-hover); }
  .provider-card.sel-anthropic { border-color: var(--anthropic-border); background: var(--anthropic-bg); }
  .provider-card.sel-openai { border-color: var(--openai-border); background: var(--openai-bg); }
  .provider-card.sel-gemini { border-color: var(--gemini-border); background: var(--gemini-bg); }
  .provider-card.sel-groq { border-color: var(--groq-border); background: var(--groq-bg); }
  .provider-card svg { width: 22px; height: 22px; flex-shrink: 0; }
  .provider-card .pname { font-size: .8rem; font-weight: 600; }
  .pname.c-anthropic { color: var(--anthropic); }
  .pname.c-openai { color: var(--openai); }
  .pname.c-gemini { color: var(--gemini); }
  .pname.c-groq { color: var(--groq); }
  .free-badge {
    font-size: .5rem; font-weight: 700; color: var(--green); background: rgba(52,211,153,0.12);
    border: 1px solid rgba(52,211,153,0.25); border-radius: 4px; padding: .1rem .35rem;
    letter-spacing: .05em; text-transform: uppercase; margin-left: .3rem;
  }

  /* Model select */
  .model-select {
    width: 100%; background: var(--bg-input); border: 2px solid var(--border);
    border-radius: 10px; color: var(--text); font-family: inherit;
    font-size: .8rem; padding: .65rem .85rem; outline: none;
    cursor: pointer; transition: border-color .2s; appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='%238994a7'%3E%3Cpath d='M6 8.5L1.5 4h9z'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right .8rem center;
  }
  .model-select:focus { border-color: var(--accent); }

  /* API key input */
  .key-wrapper { position: relative; }
  .key-input {
    width: 100%; background: var(--bg-input); border: 2px solid var(--border);
    border-radius: 10px; color: var(--text); font-family: 'JetBrains Mono', monospace;
    font-size: .8rem; padding: .65rem .85rem; padding-right: 2.5rem;
    outline: none; transition: border-color .2s;
  }
  .key-input:focus { border-color: var(--accent); }
  .key-input::placeholder { color: var(--text-muted); font-family: 'Inter', sans-serif; }
  .key-toggle {
    position: absolute; right: .6rem; top: 50%; transform: translateY(-50%);
    background: none; border: none; color: var(--text-muted); cursor: pointer;
    padding: .2rem; display: flex; transition: color .15s;
  }
  .key-toggle:hover { color: var(--text); }

  /* Security badge */
  .security-badge {
    display: inline-flex; align-items: center; gap: .45rem; margin-top: .8rem;
    padding: .5rem .85rem; border-radius: 8px;
    background: rgba(52,211,153,0.06); border: 1px solid rgba(52,211,153,0.2);
  }
  .security-badge svg { flex-shrink: 0; }
  .security-badge span {
    font-size: .72rem; color: var(--green); line-height: 1.5;
  }

  /* --- Chat area --- */
  .chat-wrap {
    flex: 1; max-width: 920px; width: 100%; margin: 0 auto;
    display: flex; flex-direction: column; padding: 1rem 1.5rem; overflow: hidden;
  }

  .messages {
    flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: .8rem;
    padding: .5rem 0 1rem; scrollbar-width: thin; scrollbar-color: var(--border) transparent;
  }
  .messages::-webkit-scrollbar { width: 5px; }
  .messages::-webkit-scrollbar-track { background: transparent; }
  .messages::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  .msg { display: flex; gap: .7rem; max-width: 100%; }
  .msg-icon {
    width: 32px; height: 32px; border-radius: 8px; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: .6rem; font-weight: 700; font-family: 'JetBrains Mono', monospace;
  }
  .msg-user .msg-icon { background: var(--surface); color: var(--text-secondary); border: 1px solid var(--border); }
  .msg-agent .msg-icon.ic-anthropic { background: var(--anthropic-bg); color: var(--anthropic); border: 1px solid var(--anthropic-border); }
  .msg-agent .msg-icon.ic-openai { background: var(--openai-bg); color: var(--openai); border: 1px solid var(--openai-border); }
  .msg-agent .msg-icon.ic-gemini { background: var(--gemini-bg); color: var(--gemini); border: 1px solid var(--gemini-border); }
  .msg-agent .msg-icon.ic-groq { background: var(--groq-bg); color: var(--groq); border: 1px solid var(--groq-border); }
  .msg-agent .msg-icon.ic-system { background: var(--accent-dim); color: var(--accent); border: 1px solid var(--accent-border); }

  .msg-content { flex: 1; min-width: 0; }
  .msg-bubble {
    background: var(--bg-raised); border: 1px solid var(--border); border-radius: 12px;
    padding: .8rem 1rem; font-size: .82rem; line-height: 1.7;
  }
  .msg-agent .msg-bubble { border-color: var(--border); }
  .msg-bubble p { margin-bottom: .5rem; }
  .msg-bubble p:last-child { margin-bottom: 0; }
  .msg-bubble code {
    background: var(--bg-input); padding: .15rem .4rem; border-radius: 4px;
    font-size: .75rem; color: var(--accent); font-family: 'JetBrains Mono', monospace;
  }
  .msg-bubble pre {
    background: var(--bg-input); border: 1px solid var(--border); border-radius: 8px;
    padding: .7rem .9rem; margin: .5rem 0; overflow-x: auto; font-size: .72rem;
    font-family: 'JetBrains Mono', monospace;
  }
  .msg-bubble pre code { background: none; padding: 0; }
  .msg-bubble strong { color: var(--text); font-weight: 600; }
  .msg-bubble ul, .msg-bubble ol { padding-left: 1.3rem; margin: .4rem 0; }
  .msg-bubble li { margin-bottom: .25rem; }
  .msg-bubble h1, .msg-bubble h2, .msg-bubble h3 {
    color: var(--accent); font-size: .85rem; margin: .7rem 0 .3rem;
  }
  .msg-meta {
    display: flex; gap: .6rem; align-items: center; margin-top: .3rem;
    font-size: .65rem; color: var(--text-muted);
  }

  .typing-dots { display: flex; align-items: center; gap: .35rem; padding: .6rem 0; }
  .typing-dots span {
    width: 6px; height: 6px; border-radius: 50%; background: var(--accent); opacity: .25;
    animation: bounce 1.4s ease-in-out infinite;
  }
  .typing-dots span:nth-child(2) { animation-delay: .2s; }
  .typing-dots span:nth-child(3) { animation-delay: .4s; }
  @keyframes bounce { 0%,100% { opacity: .25; transform: translateY(0); } 50% { opacity: 1; transform: translateY(-3px); } }

  /* --- Suggestions --- */
  .suggestions { display: flex; gap: .4rem; flex-wrap: wrap; margin-bottom: .6rem; }
  .suggestions button {
    background: var(--bg-raised); border: 1px solid var(--border); color: var(--text-secondary);
    padding: .4rem .8rem; border-radius: 8px; cursor: pointer;
    font-family: inherit; font-size: .72rem; transition: all .15s;
  }
  .suggestions button:hover { border-color: var(--accent-border); color: var(--accent); background: var(--accent-dim); }

  /* --- Input bar --- */
  .input-bar {
    flex-shrink: 0; display: flex; gap: .5rem;
    background: var(--bg-raised); border: 1px solid var(--border); border-radius: 12px;
    padding: .4rem .4rem .4rem .9rem; align-items: center;
    transition: border-color .2s;
  }
  .input-bar:focus-within { border-color: var(--accent-border); }
  .input-bar input {
    flex: 1; background: none; border: none; color: var(--text);
    font-family: inherit; font-size: .85rem; outline: none; padding: .4rem 0;
  }
  .input-bar input::placeholder { color: var(--text-muted); }
  .input-bar button {
    background: var(--accent); border: none; color: var(--bg);
    padding: .5rem 1.2rem; border-radius: 8px; font-family: 'JetBrains Mono', monospace;
    font-size: .72rem; font-weight: 600; cursor: pointer; letter-spacing: .05em;
    transition: all .15s; white-space: nowrap;
  }
  .input-bar button:hover { filter: brightness(1.15); }
  .input-bar button:disabled { opacity: .35; cursor: not-allowed; }

  @media (max-width: 768px) {
    .setup-row { flex-direction: column; }
    .chat-wrap { padding: .8rem; }
  }
</style>
</head>
<body>
<div class="top-bar">
  <div class="top-bar-left">
    <span class="sys-label">INCIDENT MONITOR</span>
    <span><span class="pulse-dot"></span>ONLINE</span>
  </div>
  <div class="top-bar-nav">
    <a href="/logs">Dashboard</a>
    <a href="/agent" class="active">Agent</a>
  </div>
  <span class="clock" id="clock">--:--:-- UTC</span>
</div>

<div class="setup-panel">
  <div class="setup-inner">
    <div class="setup-title">Configure your AI model</div>
    <div class="setup-steps">
      <div>
        <div class="step-label"><div class="step-num">1</div><span>Choose Provider</span></div>
        <div class="provider-cards" id="providerCards">
          <div class="provider-card sel-anthropic" data-provider="anthropic" onclick="selectProvider('anthropic')">
            <svg viewBox="0 0 24 24" fill="none"><path d="M13.827 3L22 21h-4.586l-1.637-3.6H9.14L16.277 3h-2.45zm-.282 4.4L9.86 14.85h6.372L13.545 7.4z" fill="var(--anthropic)"/><path d="M7.723 3H3L11.173 21h4.586L7.723 3z" fill="var(--anthropic)"/></svg>
            <span class="pname c-anthropic">Anthropic</span>
          </div>
          <div class="provider-card" data-provider="openai" onclick="selectProvider('openai')">
            <svg viewBox="0 0 24 24" fill="none"><path d="M22.282 9.821a5.985 5.985 0 0 0-.516-4.91 6.046 6.046 0 0 0-6.51-2.9A6.065 6.065 0 0 0 4.981 4.18a5.998 5.998 0 0 0-3.998 2.9 6.042 6.042 0 0 0 .743 7.097 5.98 5.98 0 0 0 .51 4.911 6.051 6.051 0 0 0 6.515 2.9A5.985 5.985 0 0 0 13.26 24a6.056 6.056 0 0 0 5.772-4.206 5.99 5.99 0 0 0 3.997-2.9 6.056 6.056 0 0 0-.747-7.073zM13.26 22.43a4.476 4.476 0 0 1-2.876-1.04l.141-.081 4.779-2.758a.795.795 0 0 0 .392-.681v-6.737l2.02 1.168a.071.071 0 0 1 .038.052v5.583a4.504 4.504 0 0 1-4.494 4.494zM3.6 18.304a4.47 4.47 0 0 1-.535-3.014l.142.085 4.783 2.759a.771.771 0 0 0 .78 0l5.843-3.369v2.332a.08.08 0 0 1-.033.062L9.74 19.95a4.5 4.5 0 0 1-6.14-1.646zM2.34 7.896a4.485 4.485 0 0 1 2.366-1.973V11.6a.766.766 0 0 0 .388.676l5.815 3.355-2.02 1.168a.076.076 0 0 1-.071 0l-4.83-2.786A4.504 4.504 0 0 1 2.34 7.872zm16.597 3.855l-5.833-3.387L15.119 7.2a.076.076 0 0 1 .071 0l4.83 2.791a4.494 4.494 0 0 1-.676 8.105v-5.678a.79.79 0 0 0-.407-.667zm2.01-3.023l-.141-.085-4.774-2.782a.776.776 0 0 0-.785 0L9.409 9.23V6.897a.066.066 0 0 1 .028-.061l4.83-2.787a4.5 4.5 0 0 1 6.68 4.66zm-12.64 4.135l-2.02-1.164a.08.08 0 0 1-.038-.057V6.075a4.5 4.5 0 0 1 7.375-3.453l-.142.08L8.704 5.46a.795.795 0 0 0-.393.681zm1.097-2.365l2.602-1.5 2.607 1.5v2.999l-2.597 1.5-2.607-1.5z" fill="var(--openai)"/></svg>
            <span class="pname c-openai">OpenAI</span>
          </div>
          <div class="provider-card" data-provider="gemini" onclick="selectProvider('gemini')">
            <svg viewBox="0 0 24 24" fill="none"><path d="M12 24A14.304 14.304 0 0 0 0 12 14.304 14.304 0 0 0 12 0a14.304 14.304 0 0 0 0 12 14.304 14.304 0 0 0 0 12z" fill="url(#gem)"/><defs><linearGradient id="gem" x1="0" y1="12" x2="24" y2="12"><stop stop-color="#4285f4"/><stop offset=".5" stop-color="#9b72cb"/><stop offset="1" stop-color="#d96570"/></linearGradient></defs></svg>
            <span class="pname c-gemini">Gemini</span>
          </div>
          <div class="provider-card" data-provider="groq" onclick="selectProvider('groq')">
            <svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" stroke="var(--groq)" stroke-width="2.5" fill="none"/><circle cx="12" cy="12" r="4" fill="var(--groq)"/><path d="M12 8V2" stroke="var(--groq)" stroke-width="2" stroke-linecap="round"/></svg>
            <span class="pname c-groq">Groq</span><span class="free-badge">FREE</span>
          </div>
        </div>
      </div>
      <div class="setup-row">
        <div style="flex:1">
          <div class="step-label"><div class="step-num">2</div><span>Select Model</span></div>
          <select class="model-select" id="modelSelect"></select>
        </div>
        <div style="flex:1.5">
          <div class="step-label"><div class="step-num">3</div><span>Enter API Key</span></div>
          <div class="key-wrapper">
            <input type="password" class="key-input" id="apiKeyInput" placeholder="Paste your API key here..." autocomplete="off">
            <button class="key-toggle" onclick="toggleKey()" title="Show/hide key">
              <svg id="eyeIcon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
            </button>
          </div>
        </div>
      </div>
    </div>
    <div class="security-badge">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--green)" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
      <span>Your key lives only in this browser tab's memory. It's sent directly to the provider API and never logged, stored, or visible to our server. Closing the tab erases it.</span>
    </div>
  </div>
</div>

<div class="chat-wrap">
  <div class="messages" id="messages">
    <div class="msg msg-agent">
      <div class="msg-icon ic-system">AG</div>
      <div class="msg-content">
        <div class="msg-bubble">
          <p><strong>Incident analysis agent ready.</strong> Configure your AI model above, then ask me anything about your monitored services.</p>
          <p>Example queries:</p>
          <ul>
            <li><strong>What broke recently?</strong> &mdash; latest incidents across all services</li>
            <li><strong>Show active incidents</strong> &mdash; unresolved issues right now</li>
            <li><strong>Summarize OpenAI outages</strong> &mdash; filter by service</li>
            <li><strong>Incident timeline</strong> &mdash; chronological breakdown</li>
          </ul>
        </div>
        <div class="msg-meta"><span>system</span></div>
      </div>
    </div>
  </div>
  <div class="suggestions">
    <button onclick="ask('What are the active incidents right now?')">Active incidents</button>
    <button onclick="ask('What broke in the last 24 hours?')">Recent outages</button>
    <button onclick="ask('Give me a summary of all services')">Service summary</button>
    <button onclick="ask('Which service has had the most incidents?')">Worst offender</button>
    <button onclick="ask('Show the incident timeline for today')">Today's timeline</button>
  </div>
  <div class="input-bar">
    <input type="text" id="input" placeholder="Ask about incidents..." autocomplete="off">
    <button id="send" onclick="send()">QUERY</button>
  </div>
</div>

<script>
// --- Clock ---
function tickClock() {
  document.getElementById("clock").textContent = new Date().toISOString().slice(11,19) + " UTC";
}
setInterval(tickClock, 1000); tickClock();

// --- Key visibility toggle ---
function toggleKey() {
  var inp = document.getElementById("apiKeyInput");
  inp.type = inp.type === "password" ? "text" : "password";
}

// --- Provider / Model ---
var models = {
  anthropic: [
    {id:"claude-haiku-4-5-20251001", name:"Haiku 4.5", tier:"Budget", price:"$0.80 / 1M tokens"},
    {id:"claude-sonnet-4-5-20250514", name:"Sonnet 4.5", tier:"Balanced", price:"$3 / 1M tokens"},
    {id:"claude-opus-4-0-20250514", name:"Opus 4", tier:"Premium", price:"$15 / 1M tokens"}
  ],
  openai: [
    {id:"gpt-4o-mini", name:"GPT-4o Mini", tier:"Budget", price:"$0.15 / 1M tokens"},
    {id:"gpt-4o", name:"GPT-4o", tier:"Balanced", price:"$2.50 / 1M tokens"},
    {id:"o3-mini", name:"o3-mini", tier:"Reasoning", price:"$1.10 / 1M tokens"}
  ],
  gemini: [
    {id:"gemini-2.0-flash-lite", name:"Flash-Lite 2.0", tier:"Budget", price:"Free tier (API key required)"},
    {id:"gemini-2.0-flash", name:"Flash 2.0", tier:"Balanced", price:"$0.10 / 1M tokens"},
    {id:"gemini-2.5-pro-preview-06-05", name:"Gemini 2.5 Pro", tier:"Premium", price:"$1.25 / 1M tokens"}
  ],
  groq: [
    {id:"llama-3.3-70b-versatile", name:"Llama 3.3 70B", tier:"Free", price:"Free (rate limited)"},
    {id:"gemma2-9b-it", name:"Gemma 2 9B", tier:"Free", price:"Free (rate limited)"},
    {id:"mixtral-8x7b-32768", name:"Mixtral 8x7B", tier:"Free", price:"Free (rate limited)"}
  ]
};
var iconLabels = {anthropic:"CL", openai:"OA", gemini:"GE", groq:"GQ"};
var currentProvider = "anthropic";

function selectProvider(p) {
  currentProvider = p;
  document.querySelectorAll(".provider-card").forEach(function(c) {
    c.className = "provider-card" + (c.dataset.provider === p ? " sel-" + p : "");
  });
  var sel = document.getElementById("modelSelect");
  sel.innerHTML = "";
  models[p].forEach(function(m) {
    var opt = document.createElement("option");
    opt.value = m.id;
    opt.textContent = m.name + "  —  " + m.tier + "  (" + m.price + ")";
    sel.appendChild(opt);
  });
  var keyInput = document.getElementById("apiKeyInput");
  keyInput.value = sessionStorage.getItem("agent_key_" + p) || "";
  var hints = {
    anthropic: "sk-ant-... (from console.anthropic.com)",
    openai: "sk-... (from platform.openai.com)",
    gemini: "AIza... (from aistudio.google.com)",
    groq: "gsk_... (free at console.groq.com)"
  };
  keyInput.placeholder = hints[p] || "Paste your API key here...";
}
document.getElementById("apiKeyInput").addEventListener("input", function() {
  sessionStorage.setItem("agent_key_" + currentProvider, this.value);
});
selectProvider("anthropic");

// --- Markdown ---
function md(text) {
  return text
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/```([\\s\\S]*?)```/g, function(m,c){ return "<pre><code>"+c.trim()+"</code></pre>"; })
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\\*\\*(.+?)\\*\\*/g, "<strong>$1</strong>")
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h2>$1</h2>")
    .replace(/^# (.+)$/gm, "<h1>$1</h1>")
    .replace(/^[\\-\\*] (.+)$/gm, "<li>$1</li>")
    .replace(/(<li>.*<\\/li>)/gs, "<ul>$1</ul>")
    .replace(/<\\/ul>\\s*<ul>/g, "")
    .split("\\n").map(function(l){
      l=l.trim();
      if(!l||l.startsWith("<h")||l.startsWith("<pre")||l.startsWith("<ul")||l.startsWith("<li")||l.startsWith("<ol")) return l;
      return "<p>"+l+"</p>";
    }).join("\\n");
}

// --- Chat ---
var messagesDiv = document.getElementById("messages");
var inputEl = document.getElementById("input");
var sendBtn = document.getElementById("send");

function addMessage(role, content, modelName) {
  var div = document.createElement("div");
  div.className = "msg msg-" + role;
  var time = new Date().toISOString().slice(11,19) + " UTC";
  var bodyHtml = role === "agent" ? md(content) : "<p>" + content.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;") + "</p>";
  if (role === "agent") {
    var icClass = "ic-" + currentProvider;
    var label = iconLabels[currentProvider] || "AG";
    var meta = '<span>' + time + '</span>' + (modelName ? '<span>via ' + modelName + '</span>' : '');
    div.innerHTML = '<div class="msg-icon '+icClass+'">' + label + '</div><div class="msg-content"><div class="msg-bubble">' + bodyHtml + '</div><div class="msg-meta">' + meta + '</div></div>';
  } else {
    div.innerHTML = '<div class="msg-icon">YOU</div><div class="msg-content"><div class="msg-bubble">' + bodyHtml + '</div><div class="msg-meta"><span>' + time + '</span></div></div>';
  }
  messagesDiv.appendChild(div);
  messagesDiv.scrollTop = messagesDiv.scrollHeight;
}

function showTyping() {
  var div = document.createElement("div");
  div.id = "typing"; div.className = "msg msg-agent";
  var icClass = "ic-" + currentProvider;
  var label = iconLabels[currentProvider] || "AG";
  div.innerHTML = '<div class="msg-icon '+icClass+'">' + label + '</div><div class="typing-dots"><span></span><span></span><span></span></div>';
  messagesDiv.appendChild(div);
  messagesDiv.scrollTop = messagesDiv.scrollHeight;
}
function hideTyping() { var el = document.getElementById("typing"); if(el) el.remove(); }

async function send() {
  var msg = inputEl.value.trim();
  if (!msg) return;
  var apiKey = document.getElementById("apiKeyInput").value.trim();
  if (!apiKey) {
    addMessage("agent", "Please enter your API key in **Step 3** above. Your key never leaves your browser except to call the provider API directly through our server.");
    return;
  }
  var model = document.getElementById("modelSelect").value;
  var modelObj = models[currentProvider].find(function(m){ return m.id === model; });
  var modelName = (modelObj ? modelObj.name : model);

  inputEl.value = "";
  sendBtn.disabled = true;
  addMessage("user", msg);
  showTyping();

  try {
    var res = await fetch("/api/agent", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ message: msg, provider: currentProvider, model: model, api_key: apiKey })
    });
    var data = await res.json();
    hideTyping();
    addMessage("agent", data.error ? "Error: " + data.error : data.reply, modelName);
  } catch(e) {
    hideTyping();
    addMessage("agent", "Connection error: " + e.message);
  }
  sendBtn.disabled = false;
  inputEl.focus();
}

function ask(q) { inputEl.value = q; send(); }
inputEl.addEventListener("keydown", function(e) {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
inputEl.focus();
</script>
</body>
</html>
"""


async def _dashboard_handler(_request: web.Request) -> web.Response:
    html = _DASHBOARD_HTML.replace("{max_recent}", str(MAX_RECENT))
    return web.Response(text=html, content_type="text/html")


async def _agent_handler(_request: web.Request) -> web.Response:
    return web.Response(text=_AGENT_HTML, content_type="text/html")


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
    app.router.add_get("/api/autoconfirm", _api_autoconfirm_handler)
    app.router.add_post("/api/agent", _agent_query_handler)
    app.router.add_get("/api/agent/models", _agent_models_handler)
    app.router.add_get("/agent", _agent_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    _log_stderr(f"Email monitor listening on port {WEBHOOK_PORT}")
    _log_stderr("  POST /webhook/email  — SendGrid Inbound Parse endpoint")
    _log_stderr("  GET  /health         — health check")
    _log_stderr("  GET  /logs           — incident dashboard")
    _log_stderr("  GET  /agent          — AI incident agent")
    _log_stderr("  GET  /api/incidents  — incidents JSON API")
    _log_stderr("  POST /api/agent      — agent query API (multi-provider)")
    _log_stderr("  [agent] providers: Anthropic, OpenAI, Gemini — user supplies own API key")

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
