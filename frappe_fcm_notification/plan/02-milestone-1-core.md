# Milestone 1 — Core send path

**Branch:** `feat/core-rewrite` off `hotfix/credential-disclosure`.
**Gate:** a transactional push is built, authorised, sent, classified and its dead tokens
deactivated — end to end, under mocks — with no legacy code left in the app.

Absorbs old Phases 1, 2 and the device/send half of Phase 4. Fixes C1–C7, C9–C16, X1
(partly), X2, X5, P2, P3.

## 1. Target layout after this milestone

```
frappe_fcm_notification/
├── fcm/
│   ├── __init__.py
│   ├── credentials.py    # per-project SA load, Redis token cache + lock
│   ├── client.py         # FCMClient: send, send_many, topic stubs
│   ├── payload.py        # message construction, 4KB guard, per-project sound/channel
│   └── errors.py         # FCM response -> OK | RETRY | DROP_TOKEN | FAIL
├── api/
│   ├── __init__.py
│   └── v1.py             # the only whitelisted surface
├── fcm_notification.py   # thin deprecation shims only
├── frappe_fcm_notification/doctype/
│   ├── fcm_project/
│   ├── push_device/
│   ├── fcm_notification_settings/   # retained: global defaults only
│   └── user_device/                 # DELETED
├── patches/v1_0/
└── tests/
```

## 2. `fcm/credentials.py`

```python
def resolve_project(name: str | None = None) -> str          # None -> the is_default row
def get_service_account_info(project: str) -> dict
def get_project_config(project: str) -> frappe._dict         # sound, channel, bundle id, limits
def get_access_token(project: str) -> str
def get_authorized_session(project: str) -> AuthorizedSession
def invalidate(project: str) -> None                         # FCM Project.on_update
```

- **Token cache:** `frappe.cache.set_value(f"fcm:token:{project}", token, expires_in_sec=3300)`.
  `set_value`/`get_value` already apply the site prefix — do not `make_key` these.
- **Stampede lock (E1 — `frappe.cache().lock` is not a Frappe API):**
  ```python
  with frappe.cache.lock(frappe.cache.make_key(f"fcm:token:lock:{project}"),
                         timeout=30, blocking_timeout=10):
      token = frappe.cache.get_value(f"fcm:token:{project}")   # re-check inside
  ```
  `lock` comes from redis-py's `Redis.lock`, which does **not** prefix by site; the
  explicit `make_key` is what keeps six sites on one Redis from colliding.
- **No DB writes, no `frappe.db.commit()`** anywhere in this module (C4, C5).
- **Per-process credential cache with cross-process invalidation (E12):** module-level
  `_CREDS: dict[str, tuple[int, dict]]` holding `(version, info)`; `version` from
  `fcm:cred_version:{project}`, bumped by `FCM Project.on_update`. One Redis GET per send
  instead of a Single read plus decrypt (C3). A doc-hook-only invalidation leaves every
  other RQ worker serving stale credentials until restart.
- **Sources:** `Site Config` reads `frappe.conf[f"fcm_service_account_{project_name}"]`;
  `DocType` reads `get_password("service_account_json")` and parses it.
- **Session:** one `AuthorizedSession` per project per process,
  `HTTPAdapter(pool_maxsize=20, pool_connections=4, max_retries=0)` (X2). Retries belong
  to the classifier, not urllib3.
- Never log the token, the credentials object, or the SA JSON.

## 3. `fcm/errors.py`

```python
class Action(StrEnum): OK / RETRY / DROP_TOKEN / FAIL
def classify(status_code, body, headers=None) -> tuple[Action, str, float]
```

Returns `(action, error_code, retry_after_sec)`. Read the code from
`body["error"]["details"][*]["errorCode"]`, falling back to `body["error"]["status"]`,
then the HTTP status.

| Condition | Action |
|-----------|--------|
| 200 | `OK` |
| 400 `INVALID_ARGUMENT`, violation on `message.token` | `DROP_TOKEN` |
| 400 other | `FAIL` — payload bug, will never succeed |
| 401 / 403 | `RETRY` once after forcing a token refresh |
| 403 `SENDER_ID_MISMATCH` | `DROP_TOKEN` — token belongs to another Firebase project |
| 404 `UNREGISTERED` | `DROP_TOKEN` |
| 429 `QUOTA_EXCEEDED` | `RETRY`, honour `Retry-After` (seconds or HTTP-date) |
| 500 / 503 `INTERNAL`, `UNAVAILABLE` | `RETRY`, exponential backoff + jitter |
| connection error / timeout | `RETRY` |

`SENDER_ID_MISMATCH` is an addition to refactor.md's table — the most common cause of
permanently-failing tokens on a multi-project bench.

## 4. `fcm/payload.py`

```python
def build_message(*, token=None, topic=None, condition=None, title, body,
                  data=None, image=None, project_config) -> dict
```

- Exactly one of `token` / `topic` / `condition`, else `frappe.ValidationError`.
- `data` values coerced to `str`; nested structures `json.dumps`-ed; `None` dropped.
- `sound` / `android_channel_id` / `apns_bundle_id` from `project_config` (C14) — no
  `cash_notification.wav`, no `com.upscape.crm` literals.
- Text cleaning replaces the regex `cleanhtml` (C12) with
  `unescape_html(strip_html_tags(...))`. **E14:** refactor.md §6.3 claims
  `strip_html_tags` "handles entities" — it does not; it is a single tag regex
  (`frappe/utils/__init__.py:442`), so `Total&nbsp;is 10 &amp; rising` reaches the device
  verbatim. Both steps are required, and `\xa0` is normalised to a space.
- Serialised size < 4096 bytes; on overflow raise naming the offending field and its size.
  Truncate nothing silently.

## 5. `fcm/client.py`

```python
class FCMClient:
    def __init__(self, project: str | None = None)
    def send(self, message: dict) -> tuple[Action, str]
    def send_many(self, messages: list[dict], max_workers: int = 15) -> list[tuple[Action, str]]
    def subscribe_topic(self, tokens, topic) -> dict      # Milestone 3
    def unsubscribe_topic(self, tokens, topic) -> dict    # Milestone 3
```

`https://fcm.googleapis.com/v1/projects/{firebase_project_id}/messages:send`, project id
from cached credentials (C3 — one read, not two). `timeout=(3.05, 10)` always.

FCM HTTP v1 has no multicast — `registration_ids` is gone, the batch endpoint is retired,
and the Admin SDK's `sendEach` just loops. All token-path throughput is our own
concurrency; that is what makes the topic path worth building in Milestone 3.

## 6. DocTypes

`FCM Project` and `Push Device` per [05-doctype-schemas.md](05-doctype-schemas.md) §1–§2,
including the E4/E5 corrections (index on `token_hash`, blank `device_id` coerced to
`None`) and the §8 permission model. Campaign-related doctypes are Milestone 2.

`FCM Project.validate`: parse the JSON, assert `type == "service_account"` and a non-empty
`private_key`, enforce exactly one `is_default`. Fail at config time, not at 3am.

## 7. `api/v1.py` — the only whitelisted file

```python
@frappe.whitelist()
def register_device(token, platform, fcm_project=None, device_id=None,
                    app_version=None, locale=None)
@frappe.whitelist()
def unregister_device(token)
@frappe.whitelist()
def send(tokens, title, body, data=None, project=None)   # <= 50 recipients
```

- No `allow_guest`, ever — that was S4.
- `register_device` is rate-limited per user and **upserts**: look up by `token_hash`,
  then by `(device_id, fcm_project)`, and update in place. Token rotation must not create
  a row (C10).
- Reject a raw APNs token (`^[0-9a-fA-F]{64}$`) with an explanatory message pointing at
  `messaging().getToken()` — see §8.
- `send` caps at 50 tokens and enqueues; `broadcast` / `campaign_status` arrive with the
  engine in Milestone 2.

## 8. Legacy removal

- **Delete the `user_device` doctype directory outright**, plus the `after_insert` iOS
  conversion and `get_ios_device_token` (C15, and S4 permanently). The legacy Instance ID
  `batchImport` server is deprecated; iOS clients call `messaging().getToken()`, which
  already returns an FCM registration token.
- Patch `patches/v1_0/drop_user_device.py`: delete the `User Device` DocType **only when
  `tabUser Device` is empty**. A fork with real rows keeps its data and gets the courtesy
  migration in Milestone 3 instead.
- `fcm_notification.py` becomes shims only — `notification_queue`, `send_fcm_notification`,
  `cleanhtml`, `user_id` — each logging
  `frappe.logger("frappe_fcm_notification").warning(...)`, never `frappe.log_error`.
  `get_fcm_credentials` and `get_cached_access_token` are **deleted**: they were the
  vulnerability, and a shim invites a fork to re-whitelist them.
- `hooks.py`: `Notification Log.after_insert` → a real handler that resolves the project
  (E10: `is_default`, overridable via an allowlisted `fcm_project_resolver` hook), fetches
  active tokens for `doc.for_user`, and enqueues **one** job for the user's devices rather
  than one per device (X1), with `enqueue_after_commit=True` (C13) and names not Documents
  (C16). Plus `FCM Project.on_update` → `fcm.credentials.invalidate`.

## 9. `dr_wash/utils/push.py` (D3 — now mandatory, not optional)

Replace the duplicated `User Device` query with a call into the new API. Keep the
fire-and-forget contract — it is called inside payment and booking transactions and must
never raise. This deletes the N1 bug rather than migrating it.

## 10. Acceptance

- `bench --site drwash migrate` clean; `FCM Project` and `Push Device` created;
  `User Device` gone (0 rows, so the patch drops it).
- AST scan: whitelisted functions exist **only** in `api/v1.py`; `allow_guest` nowhere.
- `grep -rn "iid.googleapis.com\|cash_notification.wav\|com.upscape.crm" frappe_fcm_notification/`
  → 0 hits outside `fcm_notification_settings.json` defaults and the plan docs.
- Mocked send: `UNREGISTERED` deactivates exactly the offending device and sets
  `invalidated_on`; `SENDER_ID_MISMATCH` likewise; 429 reports `RETRY` with the
  `Retry-After` value; a >4 KB payload raises before any HTTP call happens.
- Registering the same token twice updates one row; registering a 64-hex APNs token is
  rejected with the explanatory message.
- Two concurrent token mints under a flushed cache produce exactly one refresh.
- No `tabSingles` write and no `frappe.db.commit()` on the send path.
- Tests green: `test_security.py`, `test_errors.py`, `test_payload.py`.

## 11. Risks

| Risk | Mitigation |
|------|------------|
| No real credentials to validate against (D7) | Mocks throughout; `FCM Project.validate` catches malformed SA JSON at config time; one manual smoke test gates 1.0.0 |
| Deleting `User Device` breaks a fork mid-upgrade | The drop patch is conditional on an empty table; forks keep data until the Milestone 3 migration |
| `frappe.cache.lock` blocks a web request | `blocking_timeout=10`, then fall through to minting without the lock rather than raising |
