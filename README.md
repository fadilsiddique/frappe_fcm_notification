# FCM Notification for Frappe

Push notifications from Frappe/ERPNext through Firebase Cloud Messaging, using the HTTP
v1 API. Transactional sends, bulk campaigns, topic broadcasts, and a signed API for other
apps to send through.

Targets **Frappe v16** on **Python 3.14**.

---

## Features

- **Transactional push** — a Notification Log insert delivers to that user's devices.
- **Bulk campaigns** — audiences resolved as generators, split into resumable batches,
  rate limited per project. A failed batch retries on its own; a killed worker loses one
  batch, not the campaign.
- **Topic broadcasts** — one API call regardless of audience size, with subscriptions
  maintained automatically from platform, locale and role.
- **Automatic token hygiene** — FCM responses are classified, and tokens that come back
  `UNREGISTERED` or `SENDER_ID_MISMATCH` deactivate themselves.
- **Multi-project** — several Firebase projects on one bench, each with its own
  credentials, sound, channel and rate limit.
- **Scoped external API** — other apps broadcast through HMAC-signed requests bound to
  one project and an explicit list of audiences.

---

## Installation

```bash
cd /path/to/frappe-bench
bench get-app https://github.com/fadilsiddique/frappe_fcm_notification.git
bench --site yoursite install-app frappe_fcm_notification
bench --site yoursite migrate
```

---

## Configuration

### 1. Create an FCM Project

In Firebase Console → **Project Settings → Service Accounts → Generate new private key**,
download the JSON, then create an **FCM Project** in Frappe:

| Field | Value |
|-------|-------|
| Project Name | short name, e.g. `default` |
| Firebase Project Id | `project_id` from the JSON (filled in automatically if left blank) |
| Credentials Source | `DocType` or `Site Config` |
| Upload Service Account JSON | **the .json file you just downloaded** |
| Default Sound | sound bundled in your client app |
| Android Channel Id | notification channel |
| APNS Bundle Id | iOS bundle identifier |
| Rate Limit Per Second | default 500 |

Upload the file as it came from Firebase — no field-by-field copying, no replacing `\n`
by hand. It is validated on save, encrypted into Frappe's `__Auth` table, and the uploaded
file is deleted so no plaintext key is left on disk. **Stored Credential** then shows which
key is installed (client email and key id) without revealing it.

If uploading is inconvenient, paste the JSON into **…or paste the JSON here** instead.
That field is cleared before the row is written, so the key is never stored in the
`FCM Project` table itself.

**On Frappe Cloud**, prefer `Site Config`: set `credentials_source` to `Site Config` and
put the JSON in `site_config.json` under `fcm_service_account_<project name>`. The key
then never enters the database.

Exactly one project is the default; sends that do not name a project use it.

### 2. Register devices

Devices register themselves through the API, as the logged-in user:

```
POST /api/method/frappe_fcm_notification.api.v1.register_device
{
  "token": "<FCM registration token>",
  "platform": "android",       // android | ios | web
  "device_id": "<stable per install>",
  "app_version": "2.1.0",
  "locale": "en"
}
```

Registration is an upsert: the same token, or the same `device_id`, updates the existing
row instead of adding one. To retire a device:

```
POST /api/method/frappe_fcm_notification.api.v1.unregister_device
{"token": "<FCM registration token>"}
```

> **iOS:** send the token from `messaging().getToken()`. It is already an FCM
> registration token. Raw APNS tokens are rejected — see [docs/MIGRATION.md](docs/MIGRATION.md).

---

## Sending

### Transactional

Any **Notification Log** row delivers to that user's active devices automatically — no
code required. From your own app:

```python
frappe.enqueue(
    "frappe_fcm_notification.engine.dispatch.send_transactional",
    queue="short",
    tokens=tokens,           # <= 50
    title="Payment received",
    body="AED 120 for booking BK-0042",
    data={"doctype": "Sales Invoice", "docname": "SI-0042"},
)
```

Or over HTTP, as a System Manager:
`frappe_fcm_notification.api.v1.send` with `tokens`, `title`, `body`.

### Campaigns

1. Create a **Push Audience** — `All`, `Topic`, `Role`, `User Group`, `Report`,
   `User List`, or `Method`.
2. Create a **Push Campaign** with a title, body, audience and project, then **submit**
   it. Submission is the deliberate act that starts delivery.
3. Watch `status`, `sent_count`, `failed_count` and `invalid_count` on the campaign, and
   the **Push Batch** rows underneath it.

Set `scheduled_at` to have the scheduler start it later. Cancel the campaign to stop
fan-out; running batches stop at their next check.

**Audience strategy.** `All`, and anything mapped to a topic, sends as a single topic
broadcast. Everything else resolves to tokens and fans out into batches of 500. Override
per audience with `strategy_hint`.

**`Method` audiences** run a dotted path, which must be listed in an installed app's
`fcm_audience_resolvers` hook:

```python
# hooks.py of your app
fcm_audience_resolvers = ["my_app.audiences.lapsed_customers"]
```

```python
def lapsed_customers(audience, project):
    for row in frappe.db.sql("...", as_dict=True):   # yield, never build a list
        yield row.token
```

### Topics

Devices are subscribed automatically to `all`, `platform_<platform>`, `lang_<locale>` and
`role_<role>`. A nightly job reconciles drift. Send to one by creating a `Topic` audience.

---

## Sending from another app

Create a **Push API Client**, which is scoped to one project and to an explicit list of
audiences. Empty means none.

The caller authenticates to Frappe normally (`Authorization: token <key>:<secret>` on a
user holding the `Push API Client` role) and signs the request:

| Header | Value |
|--------|-------|
| `X-Push-Key` | the client's API key |
| `X-Push-Timestamp` | unix seconds, within 300s of server time |
| `X-Push-Signature` | `HMAC-SHA256("{timestamp}.{raw_body}", api_secret)`, hex |

```
POST /api/method/frappe_fcm_notification.api.v1.broadcast
{"project": "default", "audience": "All Users", "title": "...", "body": "...",
 "idempotency_key": "order-123", "callback_url": "https://example.com/hooks/push"}

202 {"campaign_id": "PC-00042", "status": "Queued"}
```

Poll `frappe_fcm_notification.api.v1.campaign_status` with the returned id, or supply
`callback_url` and receive a signed completion webhook. Repeating an `idempotency_key`
returns the original campaign rather than broadcasting twice.

These credentials are **server-to-server only** — never ship them in a mobile app.

---

## Operations

- **Push Delivery Log** records failures in full and a configurable sample of successes
  (`success_sample_rate`, default 1%). One row per recipient would out-write the sends
  themselves. Cleared after 30 days by Log Settings.
- **Rate limiting** is a Redis token bucket per project. A batch that cannot get capacity
  within `throttle_max_block_sec` requeues itself instead of holding a worker.
- **Retries** are classified, not blanket: quota and backend errors retry with backoff up
  to 5 attempts, payload errors never retry, and dead tokens deactivate.

Rough sizing for the token path: 500 tokens × 15 threads ≈ 3s per batch, so 100k
recipients across 8 workers is around 75 seconds. FCM HTTP v1 has no multicast — that
throughput is concurrency, which is why topics matter for real broadcasts.

---

## Security

The app exposes **no guest endpoints**, and `api/v1.py` is the only module containing
whitelisted functions. A test walks the AST on every run to keep it that way, and CI fails
the build otherwise. Device tokens, access tokens, credential objects and FCM response
bodies are never written to Error Log.

### Versions ≤ 0.0.1 — action required

Those versions exposed the service account over HTTP: any authenticated user, including a
self-registered Website User, could retrieve the **decrypted private key** and a live
OAuth token, and push to any device. Fixed in `0.1.0`.

Upgrading is not sufficient on its own. If your site ran `0.0.1` or earlier:

1. **Rotate the Firebase service account key** and delete the old one.
2. **Audit IAM** — the service account should hold only `roles/firebasemessaging.admin`.
3. **Purge Error Log** — older builds wrote payloads, tokens and credentials into it.

Report vulnerabilities through a GitHub Security Advisory on this repository, not a public
issue.

---

## Upgrading from 0.0.x

`bench migrate` handles it: credentials move from **FCM Notification Settings** into an
**FCM Project** named `default`, and `User Device` rows become `Push Device` rows. The
client-side iOS change is the one thing you must do yourself. See
[docs/MIGRATION.md](docs/MIGRATION.md).

---

## Troubleshooting

| Symptom | Cause |
|---------|-------|
| `No default FCM Project is configured` | No FCM Project row, or none marked default |
| Devices go inactive right after registering | Tokens are from a different Firebase project (`SENDER_ID_MISMATCH`) — check which app built them |
| iOS registrations rejected | The client is sending a raw APNS token; switch to `messaging().getToken()` |
| Campaign stuck at `Running` | Scheduler not running. Batches retry through `run_scheduled_campaigns` (every 5 min) |
| `FCM message is N bytes, over the 4096 byte limit` | Shrink `data_payload`; the error names the offending field |

Errors are logged as codes, never as response bodies — FCM echoes device tokens back in
`INVALID_ARGUMENT` responses.

---

## License

MIT.
