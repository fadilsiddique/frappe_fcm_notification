# frappe_fcm_notification — v16 Refactor & Bulk Notification Engine

**Repo:** `github.com/fadilsiddique/frappe_fcm_notification`
**Owner:** Upscape Technologies FZCO
**Target:** Frappe v16, Python 3.11+
**Audience:** Claude Code (execution agent)

---

## 0. Read this first

This is a **security fix + re-architecture** of a published app with 16 forks and a
Frappe Marketplace listing. Two consequences:

1. **Phase 0 is not optional and not deferrable.** There is a live credential
   disclosure vulnerability. Ship Phase 0 as its own commit + release before
   touching anything else.
2. **Public API compatibility matters.** Downstream forks call
   `frappe_fcm_notification.fcm_notification.notification_queue`. Deprecate with
   shims, never with silent renames.

Work phase by phase. Do not start a phase until the previous one's acceptance
criteria pass. Ask before deviating from the doctype schemas below.

---

## 1. Current state (verified by reading source)

Files reviewed: `fcm_notification.py`, `hooks.py`,
`doctype/user_device/user_device.py`, `doctype/user_device/user_device.json`,
`doctype/fcm_notification_settings/fcm_notification_settings.json`,
`pyproject.toml`, `README.md`.

**How it works today**

```
Notification Log (after_insert)
  └─ notification_queue(doc, method)
       └─ user_id(doc) → all User Device rows for doc.for_user
            └─ for each device: enqueue(send_fcm_notification, queue="default")
                 └─ get_cached_access_token()   [reads/writes Single doctype]
                 └─ get_fcm_credentials()        [second Single read, inline in f-string]
                 └─ requests.post(FCM HTTP v1)   [no timeout]
                 └─ frappe.log_error(...)        [on success AND failure]
```

Credentials live in a Single doctype `FCM Notification Settings` as nine separate
fields. `private_key` and `access_token` are `Password` fieldtype (good at rest).
The access token is cached in the same Single with an `expiration_time` datetime.

`User Device` holds `user`, `device_name`, `device_token` (Long Text),
`device_type` (ios/android/web), `is_active` (never read, never written).

iOS tokens are converted via the legacy Instance ID `batchImport` endpoint in
`UserDevice.after_insert`.

---

## 2. Defect register

Fix everything here. IDs are referenced by the phase tasks.

### Security

| ID | Severity | Issue |
|----|----------|-------|
| S1 | **Critical** | `get_fcm_credentials()` is `@frappe.whitelist()`. Any authenticated user — including self-registered Website Users on an ERPNext site — can HTTP GET the full service account JSON including the decrypted private key. |
| S2 | **Critical** | `get_cached_access_token()` is whitelisted; returns a live OAuth bearer token to any authenticated user. |
| S3 | **High** | `send_fcm_notification()` is whitelisted; any authenticated user can push arbitrary title/body to any device token. |
| S4 | **High** | `get_ios_device_token()` is `@frappe.whitelist(allow_guest=True)`. Unauthenticated callers can trigger OAuth refresh, a Single-doctype write, a `frappe.db.commit()`, and an outbound Google API call. Trivial resource-exhaustion and forced-commit vector. |
| S5 | Medium | `notification_queue()` is whitelisted with a `(doc, method)` signature. Not usefully exploitable but should not be exposed. |
| S6 | Medium | `frappe.log_error(f"Payload sent: {json.dumps(payload)}")` writes device tokens into Error Log, readable by every System Manager and included in support exports. |
| S7 | Medium | `frappe.log_error(f"Credentials: {credentials}", ...)` logs the google-auth Credentials object. Repr does not currently include the private key, but this is one library version away from doing so. Remove. |

### Correctness

| ID | Issue |
|----|-------|
| C1 | `credentials_doc.expiration_time > now()` compares `datetime` to `str` → `TypeError` on every call after the first token mint. Swallowed by the bare `except`, which returns `{"error": ...}`; the caller then does `access_token["access_token"]` → `KeyError`. Net effect: the cache likely never hits and behaviour under load is undefined. Use `frappe.utils.now_datetime()`. |
| C2 | `requests.post` has no `timeout`. A hung connection parks an RQ worker indefinitely. |
| C3 | `get_fcm_credentials()` is called twice per message (once for the token, once inline in the `fcm_endpoint` f-string) — two Single reads plus two decrypts per push. |
| C4 | `credentials_doc.save()` + `frappe.db.commit()` inside a `doc_events` hook commits the caller's partial transaction. |
| C5 | Token cache on a Single doctype = row-lock contention on `tabSingles` across concurrent workers. |
| C6 | `is_active` is never filtered in `user_id()` and never written anywhere. Dead tokens are retried forever. |
| C7 | FCM error responses are not classified. `UNREGISTERED` / `INVALID_ARGUMENT` should deactivate the device; `429`/`503` should back off and retry. |
| C8 | `frappe.log_error("Notification:", notification)` and `frappe.log_error("Notification sent successfully:", response.json())` have arguments reversed — signature is `log_error(message, title)`. Titles truncate at 140 chars. |
| C9 | 3–4 Error Log rows written per notification, including on success. At bulk volume this writes more DB rows than pushes sent. |
| C10 | `device_token` is `Long Text` — cannot carry a unique index in MariaDB. Duplicate rows accumulate on every app reinstall/token refresh. |
| C11 | `UserDevice.after_insert` calls `self.save()` → second write + Version row. Use `db_set`. |
| C12 | `cleanhtml` strips tags via regex but leaves HTML entities (`&amp;`, `&nbsp;`) in push bodies. |
| C13 | `enqueue(...)` lacks `enqueue_after_commit=True`; the job can execute before the Notification Log row is committed. |
| C14 | Client-specific constants hardcoded in a general-purpose app: `"cash_notification.wav"` (android + apns) and `"com.upscape.crm"` (bundle id). |
| C15 | `iid.googleapis.com/iid/v1:batchImport` is the deprecated legacy Instance ID server. |
| C16 | Whole `notification` Document object is passed through `enqueue` → pickled into Redis. Pass primitives. |

### Scalability

| ID | Issue |
|----|-------|
| X1 | One RQ job per device token. 50k users × 2 devices = 100k jobs, each re-reading settings and re-decrypting the key. |
| X2 | No connection reuse — a fresh TLS handshake per push. |
| X3 | No concurrency within a send unit. |
| X4 | No rate limiting or backoff; FCM will 429 and the app has no response. |
| X5 | Single Firebase project (Single doctype) — cannot serve multiple apps/clients from one bench. |
| X6 | No topic support in the shipped code path despite the README describing it. |
| X7 | No campaign/audience concept — nothing to call for bulk sends. |

### Packaging

| ID | Issue |
|----|-------|
| P1 | `requires-python = ">=3.10"` → v16 needs 3.11+. |
| P2 | `google-auth` used but not declared as a dependency. |
| P3 | `hooks.py` is untouched scaffold apart from `doc_events`. |

---

## 3. Phase 0 — Security hotfix (ship first, alone)

**Branch:** `hotfix/credential-disclosure`

### Tasks

1. Remove `@frappe.whitelist()` from `get_fcm_credentials`, `get_cached_access_token`,
   `send_fcm_notification`, `notification_queue` in `fcm_notification.py`. None of
   these need HTTP exposure — `notification_queue` is invoked by `doc_events`, the
   rest are internal. (S1, S2, S3, S5)
2. Remove `@frappe.whitelist(allow_guest=True)` from `get_ios_device_token`. Make it
   a plain module function. (S4)
3. Delete the three `frappe.log_error` calls that emit the payload, the credentials
   object, and the success response. (S6, S7, C9)
4. Add `timeout=(3.05, 10)` to both `requests.post` calls. (C2)
5. Fix C1: replace `now()` with `frappe.utils.now_datetime()` in the expiry
   comparison.
6. Fix C8: correct the reversed `log_error` argument order in what remains.
7. Bump version, tag, release.

### Out-of-band actions for the human (surface these in the PR description)

- **Rotate the Firebase service account key.** Firebase Console → Project Settings →
  Service Accounts → generate new private key, then **delete the old one**. Adding a
  new key without deleting the old leaves the compromised key valid.
- Audit Google Cloud IAM: the service account should hold **only**
  `roles/firebasemessaging.admin` (or narrower). If it has broader project roles,
  the disclosure was worse than push access.
- Check Error Log on production sites for logged payloads; purge.
- File a GitHub Security Advisory on the repo so the 16 forks are notified.
  Suggested severity: High. Affected: all versions to date.

### Acceptance

- `grep -rn "whitelist" frappe_fcm_notification/` returns only the new `api/v1.py`
  endpoints (none yet at Phase 0 — so: zero results).
- A logged-in Website User calling any of the four old endpoints gets 403/404.
- Existing notification flow still delivers end-to-end on a test site.

---

## 4. Target architecture

```
frappe_fcm_notification/
├── fcm/
│   ├── __init__.py
│   ├── credentials.py    # per-project SA load, Redis token cache + lock
│   ├── client.py         # FCMClient: send_token, send_topic, subscribe_topic
│   ├── payload.py        # message construction, 4KB guard, per-project sound/channel
│   └── errors.py         # FCM response → RETRY | DROP_TOKEN | FAIL
├── engine/
│   ├── __init__.py
│   ├── audience.py       # resolvers; decides TOPIC vs TOKEN strategy
│   ├── dispatch.py       # coordinator job + batch worker job
│   └── throttle.py       # Redis token bucket, per project
├── api/
│   ├── __init__.py
│   └── v1.py             # the only whitelisted surface
├── fcm_notification.py   # deprecated shims → engine
├── doctype/
│   ├── fcm_project/
│   ├── push_device/
│   ├── push_audience/
│   ├── push_campaign/
│   ├── push_batch/
│   ├── push_delivery_log/
│   ├── push_api_client/
│   ├── fcm_notification_settings/   # retained: global defaults only
│   └── user_device/                 # retained: read-only, migration source
└── patches/
```

### Delivery flow

```
trigger (doc_event | API | scheduled campaign)
        │
        ▼
   audience.resolve()
        │
        ├─ TOPIC strategy ──► one API call to /topics/{name} ──► done
        │
        └─ TOKEN strategy ──► Push Campaign
                                 └─► Push Batch × N   (500 tokens each)
                                       └─► worker job per batch
                                             ├─ AuthorizedSession, pool_maxsize=20
                                             ├─ ThreadPoolExecutor(max_workers=15)
                                             └─ classify each response:
                                                  200          → sent++
                                                  UNREGISTERED → device.is_active = 0
                                                  429 / 503    → honour Retry-After,
                                                                 requeue batch
                                                  other 4xx    → failed++, log row
```

**Critical design note.** FCM HTTP v1 removed `registration_ids`, and Firebase
retired the batch-send endpoint — the Admin SDK's `sendEach` merely loops. There is
**no multicast**. Throughput on the token path comes entirely from your own
concurrency. This is why the topic path matters: a true broadcast is one API call
regardless of audience size, and Google does the fan-out.

Rough sizing for the token path: 500 tokens × 15 concurrent × ~80ms ≈ 3s per batch.
100k recipients = 200 batches; 8 workers ≈ 75 seconds.

---

## 5. DocType schemas

### 5.1 FCM Project

Replaces credential storage on the Single. One row per Firebase project.

| Field | Type | Notes |
|-------|------|-------|
| `project_name` | Data | autoname field, e.g. `yallawash` |
| `firebase_project_id` | Data | reqd |
| `service_account_json` | Password | **entire JSON in one field**. Nine separate fields caused the `\n` escaping problem the README has to warn about. |
| `credentials_source` | Select | `Site Config` / `DocType`. Site Config reads `site_config.json` key `fcm_service_account_{project_name}` — key never enters the DB, preferable on Frappe Cloud. |
| `default_sound` | Data | replaces hardcoded `cash_notification.wav` (C14) |
| `android_channel_id` | Data | |
| `apns_bundle_id` | Data | replaces hardcoded `com.upscape.crm` (C14) |
| `rate_limit_per_second` | Int | default 500 |
| `is_active` | Check | default 1 |
| `is_default` | Check | exactly one enforced in `validate` |

`validate` must parse the JSON and confirm `type == "service_account"` and that
`private_key` is present — fail loudly at config time, not at 3am during a send.

### 5.2 Push Device

Replaces `User Device`. **Do not delete `User Device`** — keep it for the migration
patch and mark it deprecated in its description.

| Field | Type | Notes |
|-------|------|-------|
| `token` | Small Text | reqd. FCM tokens run ~150–300 chars — `Data`(140) is too short, `Long Text` cannot be indexed (C10). |
| `token_hash` | Data | sha256 of token, **unique index**, set in `before_insert` |
| `user` | Link → User | optional; anonymous devices are valid |
| `fcm_project` | Link → FCM Project | reqd |
| `platform` | Select | `android` / `ios` / `web` |
| `device_name` | Data | |
| `app_version` | Data | |
| `locale` | Data | `en`, `ar` — drives topic derivation |
| `device_id` | Data | stable per install; token rotation updates the row rather than inserting |
| `is_active` | Check | default 1, **actually filtered on read** (C6) |
| `last_seen` | Datetime | |
| `invalidated_on` | Datetime | set when FCM returns UNREGISTERED |
| `subscribed_topics` | Small Text | JSON list |

Indexes: unique on `token_hash`; composite on `(fcm_project, is_active)`;
index on `user`; unique on `(device_id, fcm_project)` where `device_id` is set.

### 5.3 Push Audience

| Field | Type | Notes |
|-------|------|-------|
| `audience_name` | Data | |
| `audience_type` | Select | `All` / `Topic` / `Role` / `User Group` / `Report` / `User List` / `Method` |
| `topic` | Data | for Topic type |
| `role` | Link → Role | |
| `user_group` | Link → User Group | |
| `report` | Link → Report | Query Report returning a `user` or `token` column |
| `users` | Table MultiSelect | |
| `resolver_method` | Data | dotted path; **must be validated against a `fcm_audience_resolvers` hook allowlist — never `frappe.get_attr` on arbitrary user input** |
| `strategy_hint` | Select | `Auto` / `Force Topic` / `Force Token` |

`resolve()` **must return a generator**, never a list. A 200k-row `frappe.get_all`
will OOM the worker.

### 5.4 Push Campaign

| Field | Type | Notes |
|-------|------|-------|
| `title`, `body` | Data / Text | Jinja permitted; render **once** if no personalisation tokens present, per-recipient only if they are |
| `fcm_project` | Link → FCM Project | |
| `audience` | Link → Push Audience | |
| `image_url`, `deep_link` | Data | |
| `data_payload` | JSON | |
| `scheduled_at` | Datetime | |
| `status` | Select | `Draft` / `Queued` / `Running` / `Completed` / `Failed` / `Cancelled` |
| `strategy` | Select | resolved: `Topic` / `Token` (read-only) |
| `total_recipients`, `sent_count`, `failed_count`, `invalid_count` | Int | |
| `started_at`, `completed_at` | Datetime | |
| `callback_url` | Data | webhook on completion |
| `idempotency_key` | Data | unique; caller-supplied, prevents duplicate campaigns on API retry |

Submittable. `on_submit` → enqueue coordinator.

### 5.5 Push Batch

| Field | Type |
|-------|------|
| `campaign` | Link → Push Campaign |
| `batch_index` | Int |
| `tokens` | Long Text (JSON list) |
| `status` | Select: `Pending`/`Running`/`Done`/`Failed`/`Dead` |
| `sent_count`, `failed_count`, `invalid_count`, `retry_count` | Int |
| `last_error` | Small Text |

This doctype is what makes the whole thing resumable. A failed batch retries
independently; a cancelled campaign stops enqueueing new ones.

### 5.6 Push Delivery Log

**Failures only, plus a ~1% success sample.** One row per recipient at 100k
recipients produces a table nobody can query and a write bottleneck (C9).

| Field | Type |
|-------|------|
| `campaign`, `batch` | Link |
| `device` | Link → Push Device |
| `status` | Select: `Sent`/`Failed`/`Invalid` |
| `fcm_error_code`, `error_message` | Data / Small Text |
| `sent_at` | Datetime |

Write via `frappe.db.bulk_insert`. Register in
`default_log_clearing_doctypes = {"Push Delivery Log": 30}`.

### 5.7 Push API Client

Auth for external Frappe apps. **Not** a Frappe API key — scoping is the point.

| Field | Type | Notes |
|-------|------|-------|
| `client_name` | Data | |
| `api_key` | Data | unique, indexed |
| `api_secret` | Password | HMAC-SHA256 signing key |
| `fcm_project` | Link → FCM Project | client is scoped to one project |
| `allowed_audiences` | Table MultiSelect → Push Audience | empty = none allowed, **not** all |
| `allowed_ips` | Small Text | newline-separated CIDR |
| `rate_limit_per_minute` | Int | default 60 |
| `is_active` | Check | |

Rationale: a leaked FixBuddy credential must not be able to broadcast to Dr. Wash's
user base.

---

## 6. Module specifications

### 6.1 `fcm/credentials.py`

```python
def get_service_account_info(project: str) -> dict
def get_access_token(project: str) -> str
def get_authorized_session(project: str) -> AuthorizedSession
```

- Redis cache key `fcm:token:{project}`, `expires_in_sec=3300` (55 min).
- Refresh under `frappe.cache().lock(f"fcm:token:lock:{project}", timeout=30)` so
  20 workers don't stampede Google's token endpoint (C5).
- **No DB writes, no `frappe.db.commit()`** (C4). The Single-doctype cache is gone.
- Load `service_account_json` once per project per worker process into a module-level
  dict keyed by project, invalidated by a `on_update` hook on FCM Project (C3).
- `get_authorized_session` returns a cached `AuthorizedSession` with
  `HTTPAdapter(pool_maxsize=20, max_retries=0)` — retries are handled by our own
  classifier, not urllib3 (X2).

### 6.2 `fcm/errors.py`

```python
class Action(Enum):
    OK, RETRY, DROP_TOKEN, FAIL

def classify(status_code: int, body: dict) -> tuple[Action, str]
```

Mapping (C7):

| Condition | Action |
|-----------|--------|
| 200 | `OK` |
| 400 `INVALID_ARGUMENT` where the offending field is `message.token` | `DROP_TOKEN` |
| 400 other | `FAIL` (payload bug — do not retry, it will never succeed) |
| 401 / 403 | `RETRY` once after forcing token refresh |
| 404 `UNREGISTERED` | `DROP_TOKEN` |
| 429 | `RETRY`, honour `Retry-After` header |
| 500 / 503 | `RETRY` with exponential backoff + jitter |

Read the code from `error.details[].errorCode` (the FCM v1 error envelope), not from
the HTTP status alone.

### 6.3 `fcm/payload.py`

```python
def build_message(*, token=None, topic=None, condition=None,
                  title, body, data=None, image=None,
                  project_config) -> dict
```

- Exactly one of `token` / `topic` / `condition`.
- `data` values must all be strings — coerce and document it (FCM rejects nested
  objects and non-string values).
- Sound / channel / bundle id come from `project_config`, not constants (C14).
- Use `frappe.utils.strip_html_tags` rather than the regex `cleanhtml` — it handles
  entities (C12). Keep `cleanhtml` as a deprecated alias for one release.
- Validate serialised size < 4096 bytes; raise a clear error naming the field that
  overflowed.

### 6.4 `engine/audience.py`

```python
def resolve(audience_doc, project) -> tuple[Strategy, Iterator[str] | str]
```

Returns either `(Strategy.TOPIC, topic_name)` or `(Strategy.TOKEN, token_generator)`.

Auto-selection: `All` and any audience mapping cleanly onto a maintained topic →
TOPIC. Everything else → TOKEN. `strategy_hint` overrides.

Derived topics, auto-subscribed at device registration (X6):
`all`, `platform_{android|ios|web}`, `lang_{locale}`, `role_{role_slug}`.
Maintain via the FCM batch subscribe endpoint, 1000 tokens per call.

Token generator must page with `frappe.get_all(..., limit_start=, limit_page_length=1000)`
and always filter `is_active=1` (C6).

### 6.5 `engine/dispatch.py`

```python
def dispatch_campaign(campaign_name: str)      # coordinator, queue="long"
def process_batch(batch_name: str)             # worker,     queue="long"
def send_transactional(project, tokens, title, body, data)  # ≤50 tokens, queue="short"
```

Coordinator:
- Resolve audience. Topic strategy → single send, mark Completed, return.
- Token strategy → page the generator, create Push Batch rows of 500,
  `enqueue(process_batch, ..., enqueue_after_commit=True, deduplicate=True,
  job_id=f"push-batch-{name}")` (C13, X1).
- Set `total_recipients` as batches are created, not up front (the generator doesn't
  know its length).

Worker:
- One `AuthorizedSession` for the batch (X2).
- `ThreadPoolExecutor(max_workers=15)` over the tokens (X3).
- Consult `engine/throttle.py` before each dispatch window (X4).
- Aggregate counters, **one commit at the end of the batch**.
- Deactivate DROP_TOKEN devices with a single `frappe.db.set_value` loop or bulk
  update — never one save per device.
- Retry the batch on RETRY-classified failures, up to `max_retries=5` with
  exponential backoff; then `status = Dead`.
- Never pass Document objects through `enqueue` — batch name only (C16).

### 6.6 `engine/throttle.py`

Redis token bucket keyed `fcm:throttle:{project}`, refilled at
`FCM Project.rate_limit_per_second`. Block-with-timeout rather than drop.

### 6.7 `api/v1.py`

The **only** file in the app containing `@frappe.whitelist()`.

```python
@frappe.whitelist(allow_guest=False)
def register_device(token, platform, fcm_project=None, device_id=None,
                    app_version=None, locale=None)

@frappe.whitelist(allow_guest=False)
def unregister_device(token)

@frappe.whitelist(allow_guest=False)
def send(project, tokens, title, body, data=None)      # ≤50 recipients

@frappe.whitelist(allow_guest=False)
def broadcast(project, audience, title, body, data=None,
              scheduled_at=None, idempotency_key=None, callback_url=None)

@frappe.whitelist(allow_guest=False)
def campaign_status(campaign_id)
```

Auth decorator `@require_api_client` on `send` / `broadcast` / `campaign_status`:

1. Read `X-Push-Key` / `X-Push-Signature` / `X-Push-Timestamp` headers.
2. Reject if timestamp skew > 300s (replay protection).
3. HMAC-SHA256 of `f"{timestamp}.{raw_body}"` with `api_secret`; compare with
   `hmac.compare_digest`.
4. Check source IP against `allowed_ips`.
5. Check requested `project` matches the client's `fcm_project`.
6. Check requested audience ∈ `allowed_audiences`.
7. `frappe.rate_limiter` per client per minute.

`register_device` is user-authenticated (not API-client) and rate-limited per user.
It must **not** be `allow_guest` — reintroducing that recreates S4.

`broadcast` returns `202 {"campaign_id": "PC-00042"}` immediately. Never synchronous.

### 6.8 `fcm_notification.py` — compatibility shims

Downstream forks import from here. Keep the module, keep the names, no whitelist
decorators, emit deprecation warnings:

```python
def notification_queue(doc, method=None):
    """Deprecated. Routes to engine.dispatch. Remove in 2.0."""
    frappe.log_error(...)  # NO — use frappe.logger().warning
    ...
```

Keep for one minor release: `notification_queue`, `send_fcm_notification`,
`cleanhtml`, `user_id`. Delete `get_fcm_credentials` and `get_cached_access_token`
outright — they were the vulnerability, and a shim that still returns credentials to
an internal caller is fine, but they must not remain importable under names forks
might re-expose. Prefer: move to `fcm/credentials.py` with new names, leave nothing
behind.

---

## 7. iOS token handling (C15, S4)

Delete `get_ios_device_token` and the `UserDevice.after_insert` conversion entirely.

The legacy Instance ID `batchImport` endpoint is deprecated. The correct flow: the
React Native client calls `messaging().getToken()`, which already returns an FCM
registration token on iOS — no APNs conversion, no server-side Google call, no
`allow_guest` endpoint.

Document the client-side change in the README migration notes, since existing
integrators are relying on server-side conversion.

---

## 8. Migration patches

`patches.txt`, in order:

```
frappe_fcm_notification.patches.v2_0.create_default_fcm_project
frappe_fcm_notification.patches.v2_0.migrate_user_device_to_push_device
frappe_fcm_notification.patches.v2_0.clear_legacy_token_cache
```

1. **create_default_fcm_project** — read the nine fields off the Single, reassemble
   into a service account JSON dict, write an `FCM Project` named `default` with
   `is_default=1`. Preserve the `\\n` → `\n` handling. Do not delete the Single
   fields yet; blank them in a later release once users have confirmed migration.

2. **migrate_user_device_to_push_device** — copy every `User Device` row, computing
   `token_hash`, defaulting `fcm_project` to `default`, mapping
   `device_type` → `platform`, and **setting `is_active=1` regardless of the old
   value** (the old field was never written, so its 0 default is meaningless and
   would silently deactivate every device). Deduplicate on `token_hash`, keeping the
   most recently modified row. Log the collision count.

3. **clear_legacy_token_cache** — blank `access_token` and `expiration_time` on the
   Single so no stale bearer token lingers in the DB.

Patches must be idempotent and chunked (`frappe.db.commit()` every 5000 rows).

---

## 9. Packaging & v16

- `requires-python = ">=3.11"` (P1)
- Add `dependencies = ["google-auth>=2.0.0"]` (P2)
- `hooks.py`:
  ```python
  doc_events = {
      "Notification Log": {
          "after_insert": "frappe_fcm_notification.engine.dispatch.on_notification_log"
      }
  }
  default_log_clearing_doctypes = {"Push Delivery Log": 30}
  scheduler_events = {
      "cron": {"*/5 * * * *": [
          "frappe_fcm_notification.engine.dispatch.run_scheduled_campaigns"
      ]}
  }
  fcm_audience_resolvers = []   # allowlist hook for Method-type audiences
  ```
- Verify against the actual v16 bench before finalising — v16 was still moving at
  time of writing. Flag anything that doesn't match rather than working around it.
- CI: update `.github/workflows` to a v16 matrix, Python 3.11 and 3.12.

---

## 10. Tests

Minimum bar before merging Phase 3:

- `test_security.py` — assert that no module outside `api/v1.py` contains a
  whitelisted function. Walk the AST; this is a **regression guard for the actual
  CVE** and must not be skippable.
- `test_errors.py` — table-driven over the classifier: every FCM error code maps to
  the expected Action.
- `test_audience.py` — resolvers return generators; 10k-row audience does not
  materialise a list (assert via `types.GeneratorType`).
- `test_dispatch.py` — mocked FCM: 1200 tokens produce 3 batches; a 429 requeues
  rather than fails; UNREGISTERED deactivates exactly the offending device.
- `test_payload.py` — >4KB payload raises; non-string `data` values coerced.
- `test_patches.py` — User Device → Push Device migration, including the duplicate
  token case and the `is_active` default.

---

## 11. Execution order

| Phase | Scope | Gate |
|-------|-------|------|
| **0** | Security hotfix (§3) | Ship and tag before anything else. Key rotated. Advisory filed. |
| **1** | `fcm/` package: credentials, client, payload, errors. `FCM Project` doctype + patch 1. Old flow rewired to use them. | Existing notifications still deliver. Token cached in Redis. Zero DB writes on the send path. |
| **2** | `Push Device` + patch 2. Token invalidation wired. iOS conversion deleted. | Dead tokens deactivate automatically. No duplicate device rows. |
| **3** | `engine/`: audience, dispatch, throttle. Campaign + Batch + Delivery Log doctypes. | 10k-token test campaign completes, resumable, counters accurate. |
| **4** | `api/v1.py` + `Push API Client`. Shims in `fcm_notification.py`. | External Frappe app can broadcast end-to-end. Old entry points still work with deprecation warnings. |
| **5** | Topic subscription management, scheduled campaigns, webhook callbacks. | Broadcast to `all` is a single API call. |
| **6** | README rewrite, migration guide, v16 CI. | Fresh install on clean v16 bench works from README alone. |

---

## 12. Standing constraints

- **Never** add `@frappe.whitelist()` outside `api/v1.py`.
- **Never** `allow_guest=True` anywhere in this app.
- **Never** log tokens, access tokens, or credential objects.
- **Never** `frappe.db.commit()` inside a `doc_events` hook.
- **Never** pass Document objects to `enqueue`.
- **Never** materialise an audience as a list.
- Always `timeout=` on outbound HTTP.
- Always `enqueue_after_commit=True` for document-triggered jobs.
- Keep it lean. The scope above is deliberately bounded — resist adding analytics
  dashboards, A/B testing, or a template designer in this pass.