# 08 — DocType schemas (corrected)

Single source of truth for Phases 1–4. Where this differs from refactor.md §5, the reason
is noted inline and cross-referenced to [00-analysis.md](00-analysis.md).

All doctypes live in module **Frappe Fcm Notification**, under
`frappe_fcm_notification/frappe_fcm_notification/doctype/`.

## 1. FCM Project

`autoname: field:project_name`. One row per Firebase project; ships with one row named
`default`.

| Field | Type | Notes |
|-------|------|-------|
| `project_name` | Data | reqd, unique (autoname), e.g. `yallawash` |
| `firebase_project_id` | Data | reqd — used in the send URL |
| `credentials_source` | Select | `DocType` / `Site Config`, default `DocType` |
| `service_account_file` | Attach | **primary input.** The .json as downloaded from Firebase. Read on validate, then deleted — a key in `/private/files` is a plaintext credential on disk |
| `service_account_paste` | Code (JSON) | fallback input, multi-line. Cleared in `validate` before the row is written, so it never reaches the table |
| `credentials_summary` | Data, read-only | client email + key id, so an admin can tell which key is installed without revealing it |
| `service_account_json` | Password, hidden | the encrypted store — Password fields live in `__Auth`, not in the doctype table. Written by the controller, never typed into. `__Auth.password` is `TEXT`, so ~2.5 KB fits |

**Why not a single Password field for input** (as originally specified): Password renders
as a one-line `<input type="password">`. A 2.4 KB multi-line JSON cannot be reviewed in
it, and pasting one depends on how the browser mangles newlines. Encryption and a usable
input are separate problems and need separate fields.
| `default_sound` | Data | replaces the hardcoded `cash_notification.wav` (C14) |
| `android_channel_id` | Data | |
| `apns_bundle_id` | Data | replaces the hardcoded `com.upscape.crm` (C14) |
| `rate_limit_per_second` | Int | default 500 |
| `throttle_max_block_sec` | Int | default 5 — added for E11 |
| `success_sample_rate` | Percent | default 1 — added for the delivery-log sampling policy |
| `is_active` | Check | default 1 |
| `is_default` | Check | exactly one enforced in `validate` |

`Site Config` source reads `frappe.conf["fcm_service_account_{project_name}"]` — the key
never enters the DB, preferable on Frappe Cloud.

`validate` must: parse the JSON, assert `type == "service_account"` and a non-empty
`private_key`, assert exactly one `is_default` row, and bump
`fcm:cred_version:{project}` in Redis via `on_update` (E12). Fail loudly at config time,
not at 3am during a send.

## 2. Push Device

Replaces `User Device`. `autoname: hash`.

| Field | Type | Notes |
|-------|------|-------|
| `token` | Small Text | reqd. FCM tokens run ~150–300 chars, so `Data`(140) is too short. **E4: `Small Text` maps to MariaDB `text` and is not indexable either — the index lives on `token_hash`** |
| `token_hash` | Data | sha256 hex of `token`, **unique**, set in `before_insert` (C10) |
| `user` | Link → User | optional; anonymous devices are valid |
| `fcm_project` | Link → FCM Project | reqd, default = the `is_default` project |
| `platform` | Select | `android` / `ios` / `web` |
| `device_name`, `app_version` | Data | |
| `locale` | Data | `en`, `ar` — drives topic derivation |
| `device_id` | Data | stable per install. **E5: coerce blank → `None`** so the composite unique index tolerates anonymous devices |
| `is_active` | Check | default **1**, and actually filtered on every read (C6) |
| `last_seen` | Datetime | |
| `invalidated_on` | Datetime | set when FCM returns `UNREGISTERED` / `INVALID_ARGUMENT` |
| `invalidation_reason` | Data | added — the FCM error code, for diagnosis |
| `subscribed_topics` | Small Text | JSON list; a cache of what succeeded, not truth |

Indexes: unique `token_hash`; composite `(fcm_project, is_active)`; index `user`;
composite unique `(device_id, fcm_project)` (NULL-tolerant per E5).

`search_fields` and list view must **not** show `token`. Add
`"show_preview_popup": 0` and keep the token out of `title_field`.

## 3. Push Audience

`autoname: field:audience_name`.

| Field | Type | Notes |
|-------|------|-------|
| `audience_name` | Data | reqd, unique |
| `audience_type` | Select | `All` / `Topic` / `Role` / `User Group` / `Report` / `User List` / `Method` |
| `topic` | Data | Topic type; validate against `[a-zA-Z0-9-_.~%]+` (FCM's topic charset) |
| `role` | Link → Role | |
| `user_group` | Link → User Group | |
| `report` | Link → Report | Query Report returning a `user` or `token` column |
| `users` | Table MultiSelect → **Push Audience User** | E3 — child doctype below |
| `resolver_method` | Data | dotted path, **validated against the `fcm_audience_resolvers` hook allowlist at save time**. Never `frappe.get_attr` on arbitrary user input |
| `strategy_hint` | Select | `Auto` / `Force Topic` / `Force Token`, default `Auto` |
| `fcm_project` | Link → FCM Project | scopes the audience |

`resolve()` **must return a generator**, never a list.

### 3a. Push Audience User (child, `istable: 1`) — E3

| Field | Type |
|-------|------|
| `user` | Link → User, reqd, in list view |

## 4. Push Campaign

Submittable (`is_submittable: 1`). `autoname: PC-.#####`.

| Field | Type | Notes |
|-------|------|-------|
| `title` | Data | reqd; Jinja permitted |
| `body` | Text | reqd; Jinja permitted. Render **once** unless personalisation tokens are present, per-recipient only if they are |
| `fcm_project` | Link → FCM Project | reqd |
| `audience` | Link → Push Audience | reqd |
| `image_url`, `deep_link` | Data | |
| `data_payload` | JSON | values coerced to strings by `payload.build_message` |
| `scheduled_at` | Datetime | |
| `status` | Select | `Draft`/`Queued`/`Running`/`Completed`/`Failed`/`Cancelled`, **allow_on_submit** |
| `strategy` | Select | `Topic`/`Token`, read-only, resolved at dispatch, allow_on_submit |
| `total_recipients`, `sent_count`, `failed_count`, `invalid_count` | Int | **allow_on_submit** (E6) — written with `db_set` / incremental `UPDATE`, never `save()` |
| `started_at`, `completed_at` | Datetime | allow_on_submit |
| `callback_url` | Data | https only outside developer mode; no private-range hosts (SSRF) |
| `idempotency_key` | Data | **unique**; caller-supplied, prevents duplicate campaigns on API retry |
| `error_message` | Small Text | allow_on_submit |

`on_submit` → `enqueue(dispatch_campaign, enqueue_after_commit=True, ...)`.
`on_cancel` → set `status = "Cancelled"`; running batches stop at their next check.

## 5. Push Batch

`autoname: hash`. This doctype is what makes the engine resumable.

| Field | Type |
|-------|------|
| `campaign` | Link → Push Campaign, reqd, indexed |
| `batch_index` | Int |
| `tokens` | Long Text (JSON list, ≤500) |
| `status` | Select: `Pending`/`Running`/`Done`/`Failed`/`Dead`, indexed |
| `sent_count`, `failed_count`, `invalid_count`, `retry_count` | Int |
| `next_retry_at` | Datetime |
| `last_error` | Small Text |

Composite index `(campaign, status)` — the coordinator and the completion check both
query on it.

## 6. Push Delivery Log

Failures and invalids in full, plus a ~1% success sample (`FCM Project.success_sample_rate`).
One row per recipient at 100k recipients produces a table nobody can query (C9).

| Field | Type |
|-------|------|
| `campaign` | Link → Push Campaign, indexed |
| `batch` | Link → Push Batch |
| `device` | Link → Push Device |
| `status` | Select: `Sent`/`Failed`/`Invalid` |
| `fcm_error_code` | Data |
| `error_message` | Small Text |
| `sent_at` | Datetime |

Written with `frappe.db.bulk_insert` — **E7: supply `name`, `creation`, `modified`,
`owner`, `modified_by` explicitly**. Register
`default_log_clearing_doctypes = {"Push Delivery Log": 30}` in `hooks.py`.
No `token` column, ever — the `device` link is the identifier.

## 7. Push API Client

`autoname: field:client_name`. Not a Frappe API key — scoping is the point.

| Field | Type | Notes |
|-------|------|-------|
| `client_name` | Data | reqd, unique |
| `api_key` | Data | unique, indexed, generated (`frappe.generate_hash(length=32)`) |
| `api_secret` | Password | HMAC-SHA256 signing key, generated |
| `fcm_project` | Link → FCM Project | reqd — the client is scoped to one project |
| `allowed_audiences` | Table MultiSelect → **Push API Client Audience** | E3. **Empty = none allowed, not all** |
| `allowed_ips` | Small Text | newline-separated CIDR; empty = any authenticated source |
| `rate_limit_per_minute` | Int | default 60 |
| `is_active` | Check | default 1 |

### 7a. Push API Client Audience (child, `istable: 1`) — E3

| Field | Type |
|-------|------|
| `audience` | Link → Push Audience, reqd, in list view |

## 8. Roles and permissions (N7 — absent from refactor.md)

Two new roles:

- **Push Notification Manager** — day-to-day campaign work.
- **Push API Client** — the role held by the dedicated integration user; grants nothing in
  desk, only the ability to authenticate for `api/v1`.

| DocType | System Manager | Push Notification Manager | Push API Client | All |
|---------|----------------|---------------------------|-----------------|-----|
| FCM Project | CRUD | read | — | — |
| Push Device | read, delete | read | — | — |
| Push Audience | CRUD | CRUD | — | — |
| Push Campaign | CRUD + submit/cancel | CRUD + submit/cancel | — | — |
| Push Batch | read | read | — | — |
| Push Delivery Log | read, delete | read | — | — |
| Push API Client | CRUD | — | — | — |

Notes:

- Nobody gets write on `Push Device` through the desk — devices are managed by the API.
  Read is retained for support; the list view must not surface `token`.
- `FCM Project.service_account_json` is a `Password` field, so it is never returned in a
  normal read; keep it that way and never add a client-side "reveal" button.
- `User Device` keeps its existing System Manager-only permissions and gains a
  "Deprecated — migrated to Push Device" description.
- Consider restricting submission of `All`-audience campaigns to System Manager.

## 9. Naming check

Per the bench-wide convention, these names were checked for collisions with Frappe/ERPNext
core doctypes: `FCM Project`, `Push Device`, `Push Audience`, `Push Campaign`, `Push Batch`,
`Push Delivery Log`, `Push API Client`, `Push Audience User`, `Push API Client Audience` —
none collide. Re-verify with a bench-wide grep and a site DB check before creating them;
`Campaign` alone **would** have collided with ERPNext's `Campaign`, which is why every name
here carries the `Push` prefix.
