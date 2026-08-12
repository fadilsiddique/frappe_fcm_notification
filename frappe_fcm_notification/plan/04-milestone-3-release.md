# Milestone 3 — Topics, external API, release

**Branch:** `feat/release-1.0`. **Gate:** a fresh install on a clean v16 bench works from
the README alone, and broadcasting to `all` is a single API call.

Absorbs old Phases 4 (API-client half), 5 and 6. Fixes X6, completes X7.

## 1. Topic subscription management (X6)

Derived topics, auto-subscribed at registration: `all`, `platform_{android|ios|web}`,
`lang_{locale}`, `role_{role_slug}`. Maintained via the FCM batch subscribe endpoint,
1000 tokens per call.

> Same `iid.googleapis.com` host as the deprecated `batchImport` (C15) but a different,
> still-supported operation. **Re-check Google's current guidance before implementing**
> and prefer whatever the documented topic-management endpoint is at that time. Shipping
> against a dead API is exactly how C15 happened.

`engine/topics.py`: `sync_device_topics(device_name)` on registration/update;
`reconcile_topics(project)` nightly to repair drift — `Push Device.subscribed_topics` is a
cache of what succeeded, never truth. Unsubscribe on `unregister_device` and on
`DROP_TOKEN`. Re-sync role topics on `User` role change, queued, never inline.

## 2. Scheduled campaigns

```python
scheduler_events = {"cron": {"*/5 * * * *": [
    "frappe_fcm_notification.engine.dispatch.run_scheduled_campaigns"
]}}
```

Claim each due campaign atomically —
`UPDATE ... SET status='Running' WHERE name=%s AND status='Queued'`, check `rowcount` —
so two overlapping cron runs cannot dispatch it twice. Campaigns more than N hours stale
are marked `Failed` with a reason rather than fired late after an outage.

## 3. `Push API Client` + HMAC (D2, resolving E2)

`allow_guest=False` and header-only HMAC cannot both hold: a request carrying only
`X-Push-Key` is Guest and dies at the whitelist layer before the decorator runs. Settled
model:

1. The caller authenticates as a normal Frappe API user —
   `Authorization: token <key>:<secret>` — on a dedicated user holding only the
   `Push API Client` role. Satisfies the whitelist layer with no new auth surface.
2. `@require_api_client` then does the **scoping** a Frappe API key cannot express:

   | Step | Check |
   |------|-------|
   | 1 | Resolve `Push API Client` from `X-Push-Key`; must be active |
   | 2 | `abs(now - X-Push-Timestamp) <= 300s` |
   | 3 | `hmac.compare_digest` against `HMAC_SHA256(f"{timestamp}.{raw_body}", api_secret)` |
   | 4 | Source IP ∈ `allowed_ips` |
   | 5 | Requested project == the client's `fcm_project` |
   | 6 | Requested audience ∈ `allowed_audiences` (**empty = none, not all**) |
   | 7 | `rate_limit` per client per minute |

   Failures: 403, non-specific message, one aggregated log line. Never echo the expected
   signature.

New endpoints: `broadcast` (returns `202 {"campaign_id": ...}`, never synchronous;
`idempotency_key` unique so a retry returns the existing id) and `campaign_status`
(status + counters only, never tokens).

Rationale: a leaked FixBuddy credential must not be able to broadcast to Dr Wash's users.

## 4. Completion webhooks

POST to `callback_url` on terminal status, signed with the owning client's `api_secret`
using the same HMAC scheme so the receiver can verify. `timeout=(3.05, 10)`, ≤3 attempts
with backoff, enqueued — never inline in the batch worker, and a failing webhook never
changes campaign status. Validate the URL: `https` only outside developer mode, refuse
private-range hosts, or the field is an SSRF primitive against the bench's network.

## 5. Courtesy migration for public forks

The only migration work in the plan, and it is deliberately last so it shapes nothing.
For installs that *do* have data (16 forks, Marketplace users):

- `patches/v1_0/create_default_fcm_project` — reassemble the nine Single fields **plus
  `universe_domain`** (N5) into an `FCM Project` named `default`, preserving
  `private_key.replace("\\n", "\n").strip()`. Carry `notification_sound` → `default_sound`
  and `ios_application_id` → `apns_bundle_id`. Skip when no `private_key` is set.
- `patches/v1_0/migrate_user_device_to_push_device` — copy rows, compute `token_hash`,
  map `device_type` → `platform`, **force `is_active = 1`** (the old field was never
  written; honouring its `0` default would deactivate every device), dedupe on
  `token_hash` keeping the newest, skip 64-hex APNs tokens, log the collision count.
- `patches/v1_0/clear_legacy_token_cache` — blank `access_token` / `expiration_time` and
  the corresponding `__Auth` row.

Idempotent, chunked (`frappe.db.commit()` every 5000 rows), and each one a no-op on this
bench.

## 6. Docs

README rewrite — the current one is wrong beyond the Phase 0 fix: it documents the
nine-field credential setup and the `\\n` surgery (superseded by one JSON paste), and
tells users to register devices by creating `User Device` rows (deleted). New structure:
Overview → Install → Configure an FCM Project → Register devices → Transactional sends →
Campaigns & audiences → Topics → External integration → Security → Migrating from 0.0.x →
Troubleshooting.

`docs/MIGRATION.md`, ordered by how much it hurts an integrator: rotate the key if the
site ran ≤ 0.0.1 → **iOS clients must switch to `messaging().getToken()`** → device
registration moved to `api/v1.register_device` → removed functions and the shim removal
date → what the patches do and how to verify → "nothing to do" for Notification Log-only
sites.

## 7. CI

Current workflow already targets Python 3.14 / Node 24 / MariaDB 10.6, matching the bench.
**Do not apply refactor.md §9's 3.11/3.12 matrix** (E8). Changes: fix
`on.push.branches` (`develop` → `main`, so it runs on more than PRs), bump the stale
action versions, and add a **security gate** failing the build if `@frappe.whitelist`
appears outside `api/v1.py` or `allow_guest` appears anywhere.

## 8. Release

`1.0.0`. Tag, GitHub release led by the security fix and the iOS client change, update the
Marketplace listing, resolve the advisory filed in Phase 0.

**The one manual gate (D7):** point a real Firebase project at a dev site and send one
real push. Everything else in this plan is mock-verified; this is the only step that
proves the wire format is right.

## 9. Definition of done

- [ ] Zero whitelisted functions outside `api/v1.py`; zero `allow_guest`; CI enforces both
- [ ] Zero DB writes and zero `frappe.db.commit()` on the send path
- [ ] Every outbound HTTP call has a timeout
- [ ] Dead tokens deactivate themselves; no duplicate device rows
- [ ] 100k-recipient campaign resumable and counter-accurate
- [ ] Per-project config — no client-specific constants in code
- [ ] `dr_wash` pushes deliver through the public API
- [ ] Fresh v16 install from the README alone
- [ ] One real push delivered to a real device
