# Email-Based Status Page Incident Monitor

A lightweight Python service that turns status-page incident emails into structured JSON logs and a live dashboard. It works by receiving emails through SendGrid's Inbound Parse webhook — no IMAP polling, no API keys from each status page.

```
Status page incident
  → email sent to alerts@yourdomain.com
    → SendGrid receives it (via MX records)
      → SendGrid POSTs to your server's /webhook/email
        → parsed, logged, shown on dashboard
```

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Get a Domain](#2-get-a-domain)
3. [Set Up SendGrid](#3-set-up-sendgrid)
4. [Configure DNS Records](#4-configure-dns-records)
5. [Verify DNS in SendGrid](#5-verify-dns-in-sendgrid)
6. [Set Up Inbound Parse](#6-set-up-inbound-parse)
7. [Deploy the App](#7-deploy-the-app)
8. [Test the Setup](#8-test-the-setup)
9. [Subscribe to Status Pages](#9-subscribe-to-status-pages)
10. [Using the Dashboard & API](#10-using-the-dashboard--api)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Prerequisites

- Python 3.10+
- A domain name you control (e.g. `yourdomain.com`)
- A free [SendGrid](https://sendgrid.com) account
- A server with a public URL (Railway, Render, Fly.io, a VPS, etc.)

## 2. Get a Domain

If you don't already own a domain, purchase one from any registrar:

- [GoDaddy](https://www.godaddy.com)
- [Namecheap](https://www.namecheap.com)
- [Cloudflare Registrar](https://www.cloudflare.com/products/registrar/)
- [Google Domains](https://domains.google)

A cheap `.xyz`, `.site`, or `.dev` domain works fine — this domain is only used to receive emails, not to host a website.

Once purchased, keep the registrar's DNS management panel open — you'll need it in step 4.

## 3. Set Up SendGrid

### 3.1 Create an account

1. Go to [signup.sendgrid.com](https://signup.sendgrid.com/) and create a free account.
2. Complete the email verification and account profile steps SendGrid requires.

### 3.2 Authenticate your domain (Domain Authentication)

This step lets SendGrid send/receive on behalf of your domain.

1. In the SendGrid dashboard, go to **Settings → Sender Authentication**.
2. Click **"Authenticate Your Domain"**.
3. Choose your DNS host (select GoDaddy, Namecheap, etc., or "Other" if yours isn't listed).
4. Enter your domain name (e.g. `yourdomain.com`).
5. SendGrid will generate a set of DNS records (CNAME records) you need to add. **Don't close this page yet** — you'll add these in the next step.

## 4. Configure DNS Records

Go to your domain registrar's DNS management panel. You need to add two types of records:

### 4.1 SendGrid domain authentication records (CNAME)

Add all the CNAME records that SendGrid showed you in step 3.2. They typically look like:

| Type  | Host / Name                    | Value / Points To                      |
|-------|--------------------------------|----------------------------------------|
| CNAME | `s1._domainkey.yourdomain.com` | `s1.domainkey.u12345.wl.sendgrid.net`  |
| CNAME | `s2._domainkey.yourdomain.com` | `s2.domainkey.u12345.wl.sendgrid.net`  |
| CNAME | `em1234.yourdomain.com`        | `u12345.wl.sendgrid.net`               |

> **GoDaddy-specific note:** GoDaddy auto-appends your domain to the Host field. So if SendGrid says the host is `s1._domainkey.yourdomain.com`, enter just `s1._domainkey` in GoDaddy.

> **Namecheap-specific note:** Same behaviour — enter only the subdomain part (e.g. `s1._domainkey`), Namecheap appends the domain automatically.

### 4.2 MX record (routes email to SendGrid)

This is the critical record that makes SendGrid receive emails sent to your domain.

| Type | Host / Name | Priority | Value / Points To       |
|------|-------------|----------|-------------------------|
| MX   | `@`         | 10       | `mx.sendgrid.net`      |

> **Important:** If you're using this domain for other email (e.g. Google Workspace, Outlook), adding this MX record will **redirect all email to SendGrid**. In that case, use a subdomain like `alerts.yourdomain.com` instead — set the MX record on that subdomain and use addresses like `status@alerts.yourdomain.com`.

### 4.3 Using a subdomain (recommended if domain already has email)

To avoid disrupting existing email, use a subdomain:

1. In your DNS panel, add the MX record with Host set to `alerts` (or whatever subdomain you prefer) instead of `@`.
2. In SendGrid Inbound Parse (step 6), use `alerts.yourdomain.com` as the domain.
3. Subscribe to status pages using `anything@alerts.yourdomain.com`.

### DNS propagation

DNS changes can take anywhere from a few minutes to 48 hours to propagate, though most registrars complete it within 15–30 minutes. You can check propagation status at [dnschecker.org](https://dnschecker.org).

## 5. Verify DNS in SendGrid

1. Go back to **Settings → Sender Authentication** in SendGrid.
2. Click **"Verify"** next to your domain.
3. SendGrid will check that your CNAME records are in place.
4. You should see green checkmarks once everything resolves. If not, wait for DNS propagation and retry.

## 6. Set Up Inbound Parse

This tells SendGrid to forward incoming emails to your app as HTTP POST requests.

1. In the SendGrid dashboard, go to **Settings → Inbound Parse**.
2. Click **"Add Host & URL"**.
3. Fill in:
   - **Receiving Domain**: select your authenticated domain (e.g. `yourdomain.com` or `alerts.yourdomain.com`)
   - **Destination URL**: your server's public webhook endpoint, e.g.:
     ```
     https://your-app.railway.app/webhook/email
     ```
4. Leave **"Check incoming emails for spam"** unchecked (status-page emails are not spam).
5. Leave **"Send raw, full MIME message"** unchecked (the app expects parsed fields).
6. Click **"Add"**.

## 7. Deploy the App

### Option A: Railway (recommended for quick setup)

1. Push this repo to GitHub.
2. Go to [railway.app](https://railway.app), create a new project, and connect your GitHub repo.
3. Railway will detect the `Procfile` and deploy automatically.
4. Once deployed, go to your service's **Settings → Networking → Public Networking** and generate a public domain.
5. Copy the public URL (e.g. `https://your-app.up.railway.app`) and use it as the Destination URL in SendGrid Inbound Parse (step 6).

### Option B: Render

1. Push this repo to GitHub.
2. Go to [render.com](https://render.com), create a new **Web Service**, and connect your repo.
3. Set:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python email_monitor.py`
4. Render assigns a public URL automatically — use it for SendGrid.

### Option C: Fly.io

1. Install the Fly CLI: `brew install flyctl` (or see [fly.io/docs](https://fly.io/docs/getting-started/installing-flyctl/)).
2. Run:
   ```bash
   fly launch        # creates a fly.toml — pick a region
   fly deploy
   ```
3. Set the internal port to `8081` (or set the `PORT` env var to match Fly's default `8080`):
   ```bash
   fly secrets set PORT=8080
   ```
4. Use `https://your-app.fly.dev/webhook/email` as the SendGrid destination.

### Option D: Any VPS (DigitalOcean, Linode, EC2, etc.)

```bash
# On your server
git clone <your-repo-url>
cd bolna-assignment
pip install -r requirements.txt

# Run with the port your reverse proxy expects
PORT=8081 python email_monitor.py
```

Put it behind Nginx or Caddy with HTTPS. Example Caddy config:

```
yourdomain.com {
    reverse_proxy localhost:8081
}
```

Then use `https://yourdomain.com/webhook/email` as the SendGrid destination.

### Running locally for development

```bash
pip install -r requirements.txt
python email_monitor.py
# Server starts on http://localhost:8081
```

To test with SendGrid locally, use a tunnel like [ngrok](https://ngrok.com):

```bash
ngrok http 8081
# Use the generated https URL as your SendGrid Inbound Parse destination
```

## 8. Test the Setup

### 8.1 Health check

```bash
curl https://your-app-url/health
# Should return: ok
```

### 8.2 Simulate an incoming email

```bash
curl -X POST https://your-app-url/webhook/email \
  -F "from=statuspage@example.com" \
  -F "subject=[AWS] Investigating - EC2 connectivity issues in us-east-1" \
  -F "text=We are currently investigating connectivity issues affecting EC2 instances."
```

### 8.3 Verify it worked

```bash
# Check the API
curl https://your-app-url/api/incidents

# Or open the dashboard in your browser
open https://your-app-url/logs
```

You should see the incident appear in both the JSON response and the dashboard.

### 8.4 Send a real test email

Send an email **to** any address at your domain (e.g. `test@yourdomain.com`) from your personal email with a subject like:

```
[TestPage] Investigating - Something broke
```

If everything is wired correctly, it should appear on your dashboard within a few seconds.

## 9. Subscribe to Status Pages

Now subscribe to email notifications from the status pages you want to monitor. Use any address at your domain — they all route to the same webhook.

Examples:

| Service | Status Page | Subscribe URL |
|---------|------------|---------------|
| AWS | https://status.aws.amazon.com | Subscribe via RSS-to-email or SNS |
| GitHub | https://www.githubstatus.com | Click **"Subscribe to Updates"** → Email |
| Stripe | https://status.stripe.com | Click **"Subscribe to Updates"** → Email |
| Datadog | https://status.datadoghq.com | Click **"Subscribe to Updates"** → Email |
| Vercel | https://www.vercel-status.com | Click **"Subscribe to Updates"** → Email |

Enter your monitoring address (e.g. `alerts@yourdomain.com`) when subscribing.

Most Atlassian Statuspage-powered pages send emails with subjects in the format `[Page Name] Status - Incident Title`, which this app parses automatically. For non-standard formats, the app falls back to using the sender address and raw subject.

## 10. Using the Dashboard & API

### Dashboard

Open `https://your-app-url/logs` in a browser. The dashboard:
- Auto-refreshes every 30 seconds
- Shows the last 200 incident records
- Has filter buttons to view incidents by page
- Color-codes statuses: red (investigating), yellow (identified), blue (monitoring), green (resolved)

### API

`GET /api/incidents` — returns all recent incidents as JSON.

`GET /api/incidents?page=AWS` — filter by page name (case-insensitive).

### Logs

The app writes JSON Lines files to the `logs/` directory, one file per status page:

```
logs/aws.jsonl
logs/github.jsonl
logs/stripe.jsonl
```

Each line is a JSON object:

```json
{
  "timestamp": "2025-01-15 09:23:01 UTC",
  "page": "AWS",
  "source": "email",
  "from": "noreply@notifications.statuspage.io",
  "status": "investigating",
  "incident": "EC2 connectivity issues in us-east-1",
  "detail": "We are currently investigating..."
}
```

Daily summaries are also emitted at midnight UTC:

```json
{
  "type": "daily_summary",
  "date": "2025-01-15",
  "page": "AWS",
  "total_updates": 5,
  "unique_incidents": 2,
  "message": "2 incident(s) with 5 update(s) for AWS on 2025-01-15.",
  "incidents": ["EC2 connectivity issues", "S3 elevated errors"]
}
```

## 11. Troubleshooting

| Problem | What to check |
|---------|--------------|
| Emails not arriving at your app | Verify the MX record points to `mx.sendgrid.net` — check at [mxtoolbox.com](https://mxtoolbox.com). DNS may still be propagating. |
| SendGrid Inbound Parse not triggering | Make sure the destination URL is correct and publicly accessible. Test with `curl -X POST` first. |
| 400 errors from the webhook | SendGrid must POST as `multipart/form-data` with the "Send raw" option **unchecked**. |
| Duplicates showing up | The deduplicator suppresses identical subjects within the same UTC minute. Emails arriving minutes apart are treated as separate updates (this is intentional). |
| Subject not parsed correctly | The parser expects `[Page Name] Status - Title` format. Other formats still work but the page name is derived from the sender address instead. |
| Dashboard shows no data | Records are stored in memory — if the app restarts, previous records are gone. Check `logs/*.jsonl` files for persisted history. |
| Port already in use | Set a different port: `PORT=9000 python email_monitor.py` |
