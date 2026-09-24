# DisputeFox → GoHighLevel direct sync

A one-way sync that sends new and updated DisputeFox clients into GoHighLevel. It doesn't use Zapier, Make, or n8n. It runs as a small web service you own (Python standard library, no third-party packages).

```
DisputeFox AutoFox                    this service                            GoHighLevel API v2
  "API Action"  ──POST (secret)──►  /webhooks/disputefox  ──queue──►  worker ──►  find by email / phone
  on client added / updated         validates, returns 202              (SQLite)    create or update contact
                                                                                    add tag "disputefox-synced"
```

## Why it works this way

- **DisputeFox has no public read API.** No client-list endpoint is documented, so nothing can poll DisputeFox on a schedule without inventing an endpoint. DisputeFox's documented way to push data out is the **AutoFox "API Action"**, which sends an HTTP request from a workflow step. This service is the endpoint for that request.
- **GHL side** (checked against the official OpenAPI spec, `github.com/GoHighLevel/highlevel-api-docs` → `apps/contacts.json`, API version `2021-07-28`). It uses only these endpoints:
  - `GET /contacts/search/duplicate`, called once with `email` and once with `number`
  - `POST /contacts/`
  - `PUT /contacts/{id}`
  - `POST /contacts/{id}/tags`
- **`/contacts/upsert` is deliberately not used.** When the email matches one contact and the phone matches another, upsert silently picks one. This service puts that case in a review queue instead.
- **Nothing sends messages or deletes anything.** No conversation, SMS, email, or delete endpoint exists in the code, and a test enforces that.
- **No historical import.** Only events DisputeFox sends from now on are processed.

## What it does with each client

| Situation | Result |
|---|---|
| DisputeFox client already linked to a GHL contact | Update that contact (first name, last name, email, phone) and add the tag |
| Not linked; email **or** phone matches one GHL contact | Link to it, update it, add the tag |
| Not linked; no match | Create the contact (`source: DisputeFox`) and add the tag |
| Email and phone match **two different** GHL contacts | **Review queue.** Nothing is written |
| The matching GHL contact is already linked to another DisputeFox client | **Review queue** |
| Linked, but the email or phone now belongs to a different GHL contact | **Review queue** |
| Same data arrives again | No change. Nothing is written (no duplicates, no wasted API calls) |
| GHL returns 429, 5xx, or a network error | Quick retries honoring `Retry-After`, then backoff at 30s, 2m, 8m, 32m, and 1h, then `failed` |
| GHL returns 401 or 4xx | `failed` right away (fix the cause, then run `retry`) |

If `GHL_CLIENT_ID_FIELD_ID` is set, the DisputeFox client ID is also written to that GHL custom field (for Elevated Identities: the **Dispute Fox Client ID** field), so staff can see the link on the contact.

Empty fields never overwrite existing GHL data. Payload fields other than the five it needs (for example an SSN someone adds to the AutoFox template) are discarded and never stored or logged. Logs mask emails and phone numbers and never contain the token or secret.

---

## 1. Install and test locally

Requires Python 3.10+ (`python3 --version`). There are no packages to install.

```bash
git clone https://github.com/elevatedid1-lgtm/disputefox-ghl-sync.git
cd disputefox-ghl-sync
python3 -m unittest -v            # 25 tests against a fake GHL server, no network needed
cp .env.example .env              # then edit .env (see "What I need from you")
python3 -c "import secrets;print(secrets.token_urlsafe(32))"   # paste result into WEBHOOK_SECRET
python3 -m dfghl check            # read-only: confirms token + location ID work
```

## 2. Safe rollout: dry run, then one client, then live

**Step A: dry run** (`DRY_RUN=true`, which is the default in `.env.example`). GHL is only read.

```bash
python3 -m dfghl serve &                                   # terminal 1
python3 -m dfghl send-test --email you+dftest@yourdomain.com --phone 5555550100
python3 -m dfghl events                                     # shows action=would_create / would_link_update
```

**Step B: single-client test mode.** In `.env`, set `DRY_RUN=false` and `TEST_EMAIL=you+dftest@yourdomain.com` (or `TEST_CLIENT_ID=<a DisputeFox client ID>`). Restart the service. Every other client is recorded as `skipped` and never touches GHL. Create or edit that one test client in DisputeFox, then confirm in GHL that the contact exists with the tag. Edit the client again and confirm it **updates** without creating a duplicate.

**Step C: live.** Clear `TEST_EMAIL` and `TEST_CLIENT_ID`, then restart. To replay events that were skipped during testing, run `python3 -m dfghl retry --status skipped`.

## 3. Deploy (it needs a public HTTPS URL and a persistent disk)

Run **one** instance only, because the SQLite file is the source of truth for the ID links. The host must keep `/data` (or `data/`) across restarts, or the links are lost.

**Option A: Railway or Render (Docker, easiest).**
1. Create a new service from this GitHub repo. It builds from the `Dockerfile`.
2. Attach a volume or disk mounted at **`/data`**.
3. Set the environment variables from `.env.example` in the dashboard (never in the repo).
4. Your webhook URL is `https://<your-service-domain>/webhooks/disputefox`.

`railway.json` sets the health check (`/healthz`) and pins the service to one replica. The Dockerfile deliberately has no `VOLUME` line (Railway rejects it) and runs as root so it can write to Railway's root-owned volume.

**Option B: a small Linux VPS** (about $5/month, e.g. DigitalOcean or Hetzner Ubuntu 24.04).

```bash
sudo useradd --system --home /opt/dfghl dfghl
sudo git clone https://github.com/elevatedid1-lgtm/disputefox-ghl-sync.git /opt/dfghl
sudo mkdir -p /opt/dfghl/data && sudo cp /opt/dfghl/.env.example /opt/dfghl/.env && sudo nano /opt/dfghl/.env
sudo chown -R dfghl:dfghl /opt/dfghl && sudo chmod 600 /opt/dfghl/.env
sudo cp /opt/dfghl/deploy/dfghl.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now dfghl
sudo apt install -y caddy                                    # HTTPS reverse proxy
sudo cp /opt/dfghl/deploy/Caddyfile /etc/caddy/Caddyfile && sudo nano /etc/caddy/Caddyfile   # set your domain
sudo systemctl reload caddy
sudo cp /opt/dfghl/deploy/purge.cron /etc/cron.d/dfghl-purge
journalctl -u dfghl -f                                       # live logs
```

Point a DNS `A` record (e.g. `sync.yourdomain.com`) at the VPS first so Caddy can get the certificate.

## 4. Set up DisputeFox (AutoFox → API Action)

In DisputeFox, create an **AutoFox** whose trigger is a new client being added (and a second one for client updates, if DisputeFox offers that trigger). Add a step with an **API Action**:

- **Method:** `POST`
- **URL:** `https://<your-domain>/webhooks/disputefox`
- **Auth:** if the API Action lets you add headers, add `X-Webhook-Token: <WEBHOOK_SECRET>`. If it doesn't, put the secret in the URL instead: `https://<your-domain>/webhooks/disputefox?token=<WEBHOOK_SECRET>`. Either way the connection is HTTPS, and the token is never written to logs.
- **Body** (JSON or form fields both work). Insert DisputeFox's own merge fields using its field picker:

```json
{
  "client_id":  "<DisputeFox client ID merge field>",
  "first_name": "<first name merge field>",
  "last_name":  "<last name merge field>",
  "email":      "<email merge field>",
  "phone":      "<phone/mobile merge field>"
}
```

Field names are flexible (`ClientID`, `firstName`, `Mobile`, nested `{"client": {...}}` and so on are recognized). If DisputeFox sends names this service doesn't recognize, the request gets a `422` that lists the field names it received (never the values). Map them with `FIELD_EMAIL=...` and similar settings in `.env`.

**If the AutoFox body has no client ID merge field,** set `ALLOW_EMAIL_AS_CLIENT_ID=true`. Clients are then keyed by email. It works, but if a client changes their email in DisputeFox, the new address is treated as a new client, so a client-ID field is much better.

## 5. Day-to-day commands

```bash
python3 -m dfghl status                      # counts by status, links, open reviews
python3 -m dfghl events --status failed      # what failed and why
python3 -m dfghl retry                       # requeue all failed (or: retry --id 42)
python3 -m dfghl reviews                     # conflicts waiting for a person
python3 -m dfghl resolve 3 --link <ghlContactId>   # you decide which GHL contact is right
python3 -m dfghl resolve 3 --dismiss         # leave it alone
python3 -m dfghl mappings                    # DisputeFox ID -> GHL contact ID
python3 -m dfghl unlink <dfClientId>         # remove a link (does not touch GHL)
python3 -m dfghl purge --days 30             # clear personal data from old finished events
python3 -m dfghl process                     # process due events once (the server does this automatically)
```

## Things that will bite you if you skip them

1. **GHL workflows can text clients even though this service never does.** If any GHL workflow triggers on *Contact Created* or *Tag Added: disputefox-synced*, it will fire for every synced client. Check **Automation → Workflows** before going live. Add a filter or pause anything that would message a credit-repair client without consent (CROA and TCPA exposure).
2. **Check GHL's "Allow Duplicate Contact" setting** (Settings → Business Profile). With it **off**, GHL itself blocks a second contact with the same email or phone, and the conflicting sync becomes a `failed` event with GHL's message. With it **on**, this service's review queue is what prevents duplicates.
3. **Back up the database** (`data/dfghl.sqlite3`, or `/data` on Docker). It holds the ID links plus names, emails, and phones. Keep it private.
4. **Rotate the secret if it leaks.** Change `WEBHOOK_SECRET` in both `.env` and the AutoFox action, then restart.

## Capacity

Each client event uses 3–4 GHL calls. The built-in limiter keeps the service under GHL's 100 requests per 10 seconds per location, so the ceiling is about 20 events per second, or tens of thousands a day. Even the aggressive scenario (112 offices plus a 300-rep network sending thousands of new files a month) is well under 1% of that. Volume isn't the constraint; the review queue is. Someone has to clear it daily, or conflicted clients never reach GHL.
