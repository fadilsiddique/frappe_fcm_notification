# Migrating from 0.0.x to 1.0

Ordered by how much it can hurt. Everything except step 2 happens automatically on
`bench migrate`.

---

## 1. Rotate your Firebase key if the site ever ran ≤ 0.0.1

Those versions exposed `get_fcm_credentials` and `get_cached_access_token` over HTTP to
any authenticated user, including self-registered Website Users. Anyone who called them
holds your **decrypted service account private key**, and upgrading does not take that
back.

1. Firebase Console → Project Settings → Service Accounts → **Generate new private key**.
2. Upload the new JSON to your **FCM Project** (or paste it into the paste field).
3. **Delete the old key.** Adding a new one without deleting the old leaves the exposed
   key valid.
4. Check IAM: the service account should hold only `roles/firebasemessaging.admin`, or
   narrower. Broader roles mean the disclosure reached further than push.
5. Purge Error Log — builds before `0.0.1` wrote payloads, device tokens and the
   credentials object into it, where every System Manager can read them.

---

## 2. iOS clients must change — the only manual code change

Server-side APNS conversion is **gone**. It called the retired Instance ID
`batchImport` endpoint from a `allow_guest=True` function, which let anyone
unauthenticated trigger an OAuth refresh, a database write and an outbound Google call.

**Before** — the client sent an APNS device token and the server converted it.
**Now** — the client sends the FCM token directly:

```js
import messaging from "@react-native-firebase/messaging";

const token = await messaging().getToken();   // already an FCM registration token
await api.post("frappe_fcm_notification.api.v1.register_device", {
  token,
  platform: "ios",
  device_id: deviceId,
});
```

Registering a raw APNS token (64 hex characters) now fails immediately with a message
saying so. That is deliberate: previously such tokens were accepted and simply never
delivered.

---

## 3. Device registration moved to the API

Creating `User Device` rows is no longer how devices are registered — that doctype is
removed. Use `frappe_fcm_notification.api.v1.register_device`, which upserts on the token
and on `(device_id, project)`, so token rotation updates the install instead of piling up
rows.

`is_active` on `Push Device` now defaults to **1** and is filtered on every read. On the
old doctype it defaulted to 0 and was never written by anything, so integrations that
filtered on it silently delivered to nobody.

---

## 4. Removed and deprecated functions

| Symbol | Status |
|--------|--------|
| `fcm_notification.get_fcm_credentials` | **Deleted.** Was the vulnerability. Use `fcm.credentials.get_service_account_info` |
| `fcm_notification.get_cached_access_token` | **Deleted.** Use `fcm.credentials.get_access_token` |
| `user_device.get_ios_device_token` | **Deleted.** See step 2 |
| `fcm_notification.notification_queue` | Deprecated → `engine.dispatch.on_notification_log` |
| `fcm_notification.send_fcm_notification` | Deprecated → `engine.dispatch.send_transactional` |
| `fcm_notification.cleanhtml` | Deprecated → `fcm.payload.clean_text` |
| `fcm_notification.user_id` | Deprecated → `push_device.get_active_tokens` |

The deprecated four still work and log a warning. They are removed in 2.0.

No function in this app is whitelisted any more except those in `api/v1.py`. If you were
calling one over HTTP, it now returns 403/404 — that is the fix, not a regression.

---

## 5. What the patches do

Run in this order by `bench migrate`, each idempotent:

| Patch | Effect |
|-------|--------|
| `create_default_fcm_project` | Reassembles the nine credential fields on **FCM Notification Settings** (plus `universe_domain`, which the old code dropped) into an `FCM Project` named `default`. Skipped if no private key was set or a project already exists |
| `add_push_device_indexes` | Composite index on `(fcm_project, is_active)`, unique on `(device_id, fcm_project)` |
| `migrate_user_device_to_push_device` | Copies device rows, computing `token_hash`, mapping `device_type` → `platform`, forcing `is_active = 1`, dropping duplicate tokens (newest wins) and skipping raw APNS tokens. Logs the counts |
| `clear_legacy_token_cache` | Blanks the OAuth token cached on the Single — it lives in Redis now |
| `drop_user_device` | Removes the `User Device` doctype, **only when its table is empty** |

### Verifying

```bash
bench --site yoursite migrate
bench --site yoursite console
```

```python
frappe.db.count("Push Device")                      # should match your old User Device count
frappe.db.count("Push Device", {"is_active": 0})    # should be 0 immediately after migrating
frappe.db.get_value("FCM Project", {"is_default": 1}, "firebase_project_id")
```

The migration logs `N migrated, N duplicate tokens dropped, N raw APNS tokens skipped` to
the app logger. Duplicates and skips are expected — the old schema could not enforce
uniqueness on a `Long Text` column, so most installs accumulated repeats.

---

## 6. Nothing to do

If you only ever used the Notification Log path — create a notification, it reaches the
user — that keeps working across the upgrade with no changes at all.
