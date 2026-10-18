# Self-hosting VoiceAgent

VoiceAgent runs on any Linux server with Docker. One `docker compose up -d` starts four
containers:

| Container | What it does |
|---|---|
| `caddy` | HTTPS on ports 80/443, certificates from Let's Encrypt automatically, `wss://` for Twilio |
| `app` | API, dashboard, Twilio webhooks and the live media streams |
| `worker` | campaigns, CRM outcome delivery, connector sync, data retention |
| `postgres` | the database |

## What you need

- **A server.** 1 vCPU and 2 GB of RAM handle about 3 calls at once. Any provider works (Hetzner,
  DigitalOcean, OVH, Linode, a box at home with ports forwarded). Ubuntu 24.04 is assumed below.
- **A domain or subdomain** with an `A` record pointing at the server, e.g. `voice.example.com`.
  Twilio only connects to public `https`/`wss` URLs, so this is required.
- **Ports 80 and 443 open.**
- Provider accounts, added later per workspace in the dashboard: Twilio, Deepgram, ElevenLabs, and
  Anthropic (or OpenRouter). Cal.com is optional.

## 1. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # log out and back in
```

## 2. Get VoiceAgent and configure the instance

```bash
git clone https://github.com/HamzaElSousi/voiceagent.git
cd voiceagent
cp .env.example .env
```

Generate the instance secrets. You can do this without installing Python, using the image:

```bash
docker compose run --rm --no-deps app init-instance --print --domain voice.example.com >> .env
```

This appends `SECRET_KEY`, `ENCRYPTION_KEY`, `POSTGRES_PASSWORD`, `DOMAIN` and `PUBLIC_BASE_URL`
to `.env`; delete the empty copies of those lines from the template above them. If you have Python, `pip install . && voiceagent init-instance
--domain voice.example.com` writes them for you and never overwrites existing values.

> **Back up `ENCRYPTION_KEY`.** It encrypts every workspace's provider keys. Without it, a restored
> database is useless until each workspace re-enters its keys.

The provider key lines in `.env` (`TWILIO_*`, `DEEPGRAM_API_KEY`, ...) are only for the CLI dev
mode. On a hosted instance, each workspace enters its own keys in the dashboard.

## 3. Start it

```bash
docker compose up -d
docker compose logs -f app      # wait for "Uvicorn running"
```

The first start builds the image (a few minutes). To use the published image instead, set
`VOICEAGENT_IMAGE=ghcr.io/hamzaelsousi/voiceagent:latest` in `.env` and run `docker compose pull`
first.

## 4. Create your account

Open `https://voice.example.com`. The first account created becomes the instance admin. After
that, sign-ups are closed unless `ALLOW_SIGNUPS=true`. You can also create the admin from the
shell:

```bash
docker compose run --rm app create-admin
```

## 5. Set up a workspace

The dashboard's Overview page walks you through it:

1. **Settings > Provider keys:** Twilio SID, auth token and number, Deepgram, ElevenLabs,
   Anthropic. Optionally Cal.com.
2. **Agents > New agent:** start from the outbound or inbound template.
3. **Call me now** on the agent page, to hear it on your own phone.
4. **Connectors:** connect your CRM ([guide](CONNECTORS.md)) or import a CSV.
5. **Settings > Phone numbers:** add your Twilio number to have an inbound agent answer it. With
   "point the webhook automatically" checked, VoiceAgent sets the number's voice URL on Twilio.

## Operating it

**Backups.** Back up the database and `.env` (it holds `ENCRYPTION_KEY`):

```bash
# /etc/cron.d/voiceagent-backup: nightly at 03:15
15 3 * * * root cd /opt/voiceagent && docker compose exec -T postgres pg_dump -U voiceagent voiceagent | gzip > /var/backups/voiceagent-$(date +\%F).sql.gz
```

Restore with `gunzip -c backup.sql.gz | docker compose exec -T postgres psql -U voiceagent voiceagent`.

**Upgrading.**

```bash
git pull
docker compose up -d --build     # migrations run automatically when the app starts
```

**Useful commands.**

```bash
docker compose ps
docker compose logs -f app worker
docker compose run --rm app reset-password   # forgot a password (no email needed)
docker compose run --rm app db-upgrade       # apply migrations by hand
```

**Data retention.** Each workspace sets how long calls and transcripts are kept (Settings >
Workspace, default 90 days). The worker deletes older calls hourly. Deleting a single call is also
available on its page.

**Capacity.** `MAX_CONCURRENT_CALLS` (default 3) caps simultaneous calls across all workspaces.
Each call holds three streaming connections (Twilio, Deepgram, ElevenLabs) plus LLM requests;
raise the limit with the server size.

## Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `DOMAIN` | (required) | Public hostname; Caddy gets its certificate. |
| `PUBLIC_BASE_URL` | `https://$DOMAIN` | Set by compose. Used for Twilio webhooks and stream URLs. |
| `SECRET_KEY` | (required) | Signs per-call stream tokens. |
| `ENCRYPTION_KEY` | (required) | Fernet key that encrypts provider keys. Back it up. |
| `POSTGRES_PASSWORD` | (required) | Database password. |
| `ALLOW_SIGNUPS` | `false` | Let anyone create an account and workspace. |
| `MAX_CONCURRENT_CALLS` | `3` | Instance-wide limit on simultaneous calls. |
| `ALLOW_PRIVATE_CONNECTOR_URLS` | `false` | Let connectors reach private or loopback addresses (a CRM on the same box or LAN). |
| `SESSION_DAYS` | `14` | How long a dashboard login lasts. |
| `COOKIE_SECURE` | on for https | Secure-only cookies. Leave on. |
| `VOICEAGENT_IMAGE` | GHCR latest | Image used by `app` and `worker`. |

## Troubleshooting

**Caddy cannot get a certificate.** The `A` record must point at this server, and ports 80 and
443 must be reachable from the internet. `docker compose logs caddy` shows the ACME error.

**Calls connect but the agent is silent.** Twilio could not open the `wss://` stream. Check
`PUBLIC_BASE_URL` matches the domain exactly, and that `https://voice.example.com/health` loads
from outside.

**Twilio webhooks return 403.** VoiceAgent verifies `X-Twilio-Signature` with the workspace's
Twilio auth token. Make sure the token saved in Settings is the current one for the account that
owns the number, and that `PUBLIC_BASE_URL` matches the URL configured on Twilio.

**A campaign shows "paused: missing keys".** Add the listed keys in Settings, then press Resume.

**Nothing is being dialed.** Leads need recorded consent, a number not on the DNC list, and must
be inside their local calling hours (always within 8am to 9pm). The Leads page shows why each lead
is blocked.

## Security notes

- Provider keys are encrypted at rest and never returned by the API.
- Passwords use argon2. Sessions are random tokens, stored hashed.
- Workspaces are isolated: every query is scoped, and another tenant's ids return 404.
- Twilio callbacks are signature-checked. Media streams need a per-call token signed with
  `SECRET_KEY`.
- Connector URLs cannot target private networks unless you allow it.
- Report vulnerabilities privately: see [SECURITY.md](../SECURITY.md).
