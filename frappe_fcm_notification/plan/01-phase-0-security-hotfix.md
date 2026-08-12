# Phase 0 — Security hotfix ✅ SHIPPED

> **Status: complete.** Implemented on `hotfix/credential-disclosure`, commits `4b947f3`
> (security) and `5de8b43` (packaging/docs), tagged `v0.1.0`. Outstanding: merge forward
> into `main`, push, and the §4 human actions — of which only the advisory still applies
> on this bench (see [00-analysis.md](00-analysis.md) §6: no credentials were ever
> configured here, so there is no key to rotate locally).


**Branch:** `hotfix/credential-disclosure`, cut from **`version-16`** (not `main` — see
00-analysis N4). **Version:** `0.1.0` (D1). Ships alone, tagged, before any Phase 1 work.

**Goal:** close the credential disclosure without changing behaviour for existing
integrators. No architecture, no new doctypes, no renames.

Scope shrank from refactor.md §3 because S6, S7 and C8 were already fixed in `2b62cc4`.

## 1. Code tasks

### T0.1 — Remove all HTTP exposure (S1, S2, S3, S5)

`frappe_fcm_notification/fcm_notification.py` — delete the `@frappe.whitelist()`
decorator (and the now-unused import if any) at lines **22, 43, 63, 98**:

| Line | Function | After |
|------|----------|-------|
| 22 | `notification_queue(doc, method)` | plain function — invoked by `doc_events` |
| 43 | `get_fcm_credentials()` | internal |
| 63 | `get_cached_access_token()` | internal |
| 98 | `send_fcm_notification(notification, device_token)` | internal; still reachable via `frappe.enqueue` (N2) |

### T0.2 — Remove the guest endpoint (S4)

`.../doctype/user_device/user_device.py:19` — drop
`@frappe.whitelist(allow_guest=True)` from `get_ios_device_token`. Keep the function
(it is deleted in Phase 2, not here).

### T0.3 — Timeouts on every outbound call (C2)

- `fcm_notification.py:148` → `requests.post(fcm_endpoint, headers=headers, json=payload, timeout=(3.05, 10))`
- `user_device.py:39` → `requests.post(url, headers=headers, data=payload, timeout=(3.05, 10))`

Wrap both in `except requests.RequestException` and return the existing
`{"status": "failed", …}` shape — a timeout must not raise out of an RQ job into a
retry storm.

### T0.4 — Fix the expiry comparison (C1)

`fcm_notification.py:71`:

```python
from frappe.utils import get_datetime, now_datetime   # keep `now`/`add_to_date` for the write path

expires_at = get_datetime(credentials_doc.expiration_time) if credentials_doc.expiration_time else None
if credentials_doc.access_token and expires_at and expires_at > now_datetime():
    return {"access_token": credentials_doc.get_password("access_token")}
```

Both guards matter: the Singles cache hands the field back as `str`, and
`get_datetime("")` returns `None` (`frappe/utils/data.py:150-171`) — comparing that to a
datetime raises the same `TypeError` C1 is about. Keep `add_to_date(now(), minutes=55)`
for the write; it stores fine.

### T0.5 — Bound the error logging (C9, partial)

Leave the two remaining `log_error` calls but make them cheap and non-leaky:

- `fcm_notification.py:155` — log `response.status_code` and the FCM `error.status` /
  `error.details[].errorCode` only. **Never** `response.text` verbatim (it echoes the
  token in `INVALID_ARGUMENT` responses) and never the payload.
- `user_device.py:47,49` — same treatment.

Full classification lands in Phase 1 (`fcm/errors.py`); this is only about not writing
tokens into Error Log.

### T0.6 — Kill the README instruction (N3)

`README.md:94` — delete the "Optionally you can use whitelisted method
`api/method/frappe_fcm_notification.fcm_notification.notification_queue` to test in
postman" line. Replace with: notifications are triggered by Notification Log inserts;
there is no HTTP endpoint. Add a short **Security** section pointing at the advisory and
the key-rotation requirement.

### T0.7 — Declare the dependency (P2)

`pyproject.toml` → `dependencies = ["google-auth>=2.48.0"]`. **Leave
`requires-python = ">=3.14,<3.15"` alone** (P1 is inverted — 00-analysis E8).

### T0.8 — Version bump

`frappe_fcm_notification/__init__.py` → `__version__ = "0.1.0"`.

## 2. Explicitly out of scope for Phase 0

C3 (double credential read), C4 (commit in hook), C5 (Single-doctype cache), C6/N1
(`is_active`), C10–C16, all of X, P3. They are real, but each one changes behaviour or
schema and would delay a security release. They are Phase 1–3.

## 3. Acceptance

```bash
cd apps/frappe_fcm_notification

# 1. No whitelisted surface anywhere in the app
grep -rn "whitelist" frappe_fcm_notification/          # expect: 0 results

# 2. No timeout-less outbound calls
grep -rn "requests.post" frappe_fcm_notification/      # expect: every hit has timeout=

# 3. README no longer advertises the endpoint
grep -n "api/method" README.md                          # expect: 0 results
```

On the `drwash` site:

1. `bench --site drwash migrate && bench --site drwash restart`.
2. As a **Website User**, `POST /api/method/frappe_fcm_notification.fcm_notification.get_fcm_credentials`
   → expect `403` / `404`, and the same for the other three former endpoints and for
   `get_ios_device_token` **while logged out**.
3. Create a `User Device` row for a test user with a real token, trigger a Notification
   Log insert for that user, confirm the push arrives on the device.
4. Error Log gains no row containing a device token or a bearer token for either the
   success or the failure path.

> Note on step 3: `dr_wash`'s `push_to_customer` path will still deliver nothing,
> because of N1 (`is_active` filter vs. never-written field). That is expected here and
> is fixed in Phase 2. Test the Notification Log path, not the Dr Wash path.

## 4. Human actions — put these in the PR description

- [ ] **Rotate the Firebase service account key.** Firebase Console → Project Settings →
      Service Accounts → generate a new private key, update FCM Notification Settings,
      then **delete the old key**. Adding without deleting leaves the exposed key valid.
- [ ] **Audit IAM** for that service account: it should hold only
      `roles/firebasemessaging.admin` (or narrower). Broader project roles mean the
      disclosure was worse than push access.
- [ ] **Purge Error Log** on every production site for rows containing payloads,
      credentials objects or tokens (pre-`2b62cc4` installs wrote all three):
      `Error Log` list → filter on the old titles (`FCM Debugging`, `FCM Credentials
      Object`, `Notification:`) → delete. Also check any support exports already shared.
- [ ] **File a GitHub Security Advisory** on `fadilsiddique/frappe_fcm_notification` so
      the 16 forks are notified. Severity High. Affected: all versions ≤ 0.0.1. Include
      "rotate your service account key" as the primary remediation — updating the app
      alone does not un-leak a key that has already been fetched.
- [ ] Consider whether the Marketplace listing needs an update notice.

## 5. Risks

| Risk | Mitigation |
|------|------------|
| A fork or client app calls the removed HTTP endpoints | They were never documented except `notification_queue` (N3), which takes `(doc, method)` and cannot be usefully called over HTTP. Call it out in the advisory. |
| `dr_wash` breaks | It does not — `frappe.enqueue` resolves by `frappe.get_attr` with no whitelist check (`frappe/utils/background_jobs.py:239-256`). Verified. |
| `get_datetime("")` returns `None` and re-raises C1 | Explicit truthiness guard in T0.4 before the comparison. |
| The bare `except` in `get_cached_access_token` keeps swallowing real errors | Left in place for Phase 0 (behaviour-preserving); replaced by `fcm/credentials.py` in Phase 1. |
