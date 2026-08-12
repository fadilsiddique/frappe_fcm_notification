# 00 — Analysis: refactor.md vs. the code on disk

Verification date: 2026-08-10. Everything below was checked against source, not assumed.

## 1. Ground truth

| Fact | Value | Evidence |
|------|-------|----------|
| App branch | `version-16` @ `2b62cc4` ("version-16 upgrade"); `main` is 1 commit behind | `git log`, `git branch` |
| Remotes | only `upstream` → `github.com/fadilsiddique/frappe_fcm_notification`; no `origin` | `git remote -v` |
| App version | `0.0.1` | `frappe_fcm_notification/__init__.py:1` |
| Bench Python | **3.14.0** (`env/lib/python3.14`) | `env/bin/python -V` |
| Frappe | **16.18.2** | `frappe.__version__` |
| redis-py | 7.1.0 | venv |
| google-auth | 2.48.0, **declared by Frappe itself** (`google-auth~=2.48.0`) | `apps/frappe/pyproject.toml:88` |
| Sites with the app installed | **`drwash` only** | `bench --site all list-apps` |
| `tabUser Device` rows on drwash (this dev copy) | 0 | direct query |
| Downstream in-bench caller | `dr_wash/dr_wash/utils/push.py` | see N1/N2 |

`pyproject.toml` already declares `requires-python = ">=3.14,<3.15"` and CI already
pins Python 3.14 / Node 24. The bench matches. **Do not touch the Python floor.**

## 2. Defect register — verdict per ID

Legend: **CONFIRMED** = still present · **DONE** = already fixed on `version-16` ·
**PARTIAL** = partly addressed · **WRONG** = the claim does not match the code.

### Security

| ID | Verdict | Evidence / note |
|----|---------|-----------------|
| S1 | CONFIRMED | `fcm_notification.py:43` `@frappe.whitelist()` on `get_fcm_credentials` — returns decrypted `private_key` (line 53) to any authenticated user |
| S2 | CONFIRMED | `fcm_notification.py:63` on `get_cached_access_token` |
| S3 | CONFIRMED | `fcm_notification.py:98` on `send_fcm_notification` |
| S4 | CONFIRMED | `user_device.py:19` `@frappe.whitelist(allow_guest=True)` on `get_ios_device_token`; still does token refresh → Single write → `frappe.db.commit()` → outbound Google call, unauthenticated |
| S5 | CONFIRMED | `fcm_notification.py:22` on `notification_queue` |
| S6 | **DONE** | payload `log_error` removed in `2b62cc4` |
| S7 | **DONE** | credentials-object `log_error` removed in `2b62cc4` |

Five whitelisted entry points remain, one of them guest-accessible. Phase 0 stands as
written; its scope shrinks only by the two logging items.

### Correctness

| ID | Verdict | Evidence / note |
|----|---------|-----------------|
| C1 | CONFIRMED | `fcm_notification.py:71` — `credentials_doc.expiration_time > now()` compares `datetime` to `str` |
| C2 | CONFIRMED | `fcm_notification.py:148` and `user_device.py:39` — no `timeout=` |
| C3 | CONFIRMED | credentials read at `fcm_notification.py:115` (via token) and again inline at `:147` |
| C4 | CONFIRMED | `fcm_notification.py:88-89` `save()` + `commit()` reached from the `after_insert` hook chain |
| C5 | CONFIRMED | token cached on the Single |
| C6 | CONFIRMED — **and now an outage, see N1** | `user_id()` at `:15-20` does not filter `is_active`; nothing writes it |
| C7 | CONFIRMED | `:150-156` — status-code-only branch, no classification |
| C8 | **DONE** | argument order corrected in `2b62cc4` |
| C9 | **PARTIAL** | success/payload/debug logs gone; two error-path `log_error` calls remain (`:95`, `:155`) plus two in `user_device.py`. No longer "3–4 rows per notification"; still unbounded on a bad-token storm |
| C10 | CONFIRMED | `user_device.json:29` `device_token` is `Long Text` |
| C11 | CONFIRMED | `user_device.py:16-17` `self.save()` inside `after_insert` |
| C12 | CONFIRMED | `fcm_notification.py:9-12` regex `cleanhtml`, entities survive |
| C13 | CONFIRMED | `fcm_notification.py:27-33` `enqueue(...)` without `enqueue_after_commit` |
| C14 | **PARTIAL** | `notification_sound` and `ios_application_id` are now Single fields with a backfill patch (`patches/v0_0_1/set_default_fcm_config.py`); the client-specific values survive as literal defaults in `fcm_notification_settings.json:77,84` and as `or "…"` fallbacks at `fcm_notification.py:111` / `user_device.py:25`. Per-project config (Phase 1) still required |
| C15 | CONFIRMED | `user_device.py:23` legacy `iid.googleapis.com/iid/v1:batchImport` |
| C16 | CONFIRMED | `fcm_notification.py:32` passes the `Notification Log` Document into `enqueue` |

### Scalability

X1–X7: all **CONFIRMED**. Nothing in `2b62cc4` touches the delivery architecture.

### Packaging

| ID | Verdict | Note |
|----|---------|------|
| P1 | **WRONG / INVERTED** | refactor.md says "`>=3.10` → bump to 3.11+". Actual file says `>=3.14,<3.15`, matching the 3.14.0 bench and 3.14 CI. **Bumping down to 3.11 would be a regression.** The §9 recommendation of a "3.11 and 3.12" CI matrix is also wrong for this bench — see E8 |
| P2 | CONFIRMED (low severity) | `google-auth` is imported but undeclared; it currently resolves only because Frappe declares it. Declare it anyway — a Frappe dep drop would break this app silently |
| P3 | CONFIRMED | `hooks.py` is scaffold + `doc_events` only |

**Net:** of 30 IDs, 3 are already fixed (S6, S7, C8), 2 are partial (C9, C14), 1 is
inverted (P1), 24 stand.

## 3. Findings not in refactor.md

**N1 — Dr Wash push is dead code today.** `dr_wash/dr_wash/utils/push.py:18-22` filters
`{"user": user, "is_active": 1}` on `User Device`. `is_active` defaults to `0`
(`user_device.json:39-43`) and is never written by anything. Every
`push_to_customer` / `push_to_user` call therefore resolves zero devices and silently
sends nothing. This turns C6 from a hygiene item into a real defect.

> **Amended (§6):** with zero `User Device` rows and no configured credentials, this is a
> latent bug in an unreached path, not a live outage. It is fixed by construction in
> Milestone 1 rather than by a migration patch.

**N2 — The shim contract is stricter than refactor.md assumes.** `push.py:24-34`
enqueues `frappe_fcm_notification.fcm_notification.send_fcm_notification` **by dotted
string path** with `notification=frappe._dict(subject=…, email_content=…,
document_type=…, document_name=…)` and `device_token=<str>`. So the shim must:
accept a plain `dict`/`_dict` (not only a Document), keep the exact keyword names
`notification` and `device_token`, and keep working when resolved via `frappe.get_attr`.
Verified: `execute_job` resolves job methods with `frappe.get_attr` and performs **no**
whitelist check (`frappe/utils/background_jobs.py:239-256`), so removing
`@frappe.whitelist()` in Phase 0 does not break this caller.

**N3 — The README advertises the vulnerability.** `README.md:94` tells users to call
`api/method/frappe_fcm_notification.fcm_notification.notification_queue` from Postman.
That line must die in Phase 0, not in the Phase 6 README rewrite. (`README.md:100`
already claims payloads are not logged — true as of `2b62cc4`.)

**N4 — Branch/remote reality.** Work is on `version-16`; `main` is behind; there is no
`origin`. `hotfix/credential-disclosure` must branch off `version-16` (which is what is
actually deployed to drwash), then be merged forward into `main` — branching off `main`
would resurrect the deleted debug logging.

**N5 — `universe_domain` is dropped on the floor.** The Single has the field
(`fcm_notification_settings.json:72-75`) but `get_fcm_credentials()` never includes it.
Patch 1 must carry it into the reassembled service-account JSON; google-auth 2.48
honours it and non-default universes will otherwise break.

**N6 — Dead field.** `service_account` (Data, `fcm_notification_settings.json:91-94`) is
written by nobody and read by nobody. Blank/remove it with the Phase 1 patch.

**N7 — No permission design anywhere in refactor.md.** `Push Device` stores push
credentials; `Push API Client` stores an HMAC secret; `Push Campaign` can broadcast to
every user on the site. `User Device` today is System Manager-only. Roles and DocPerms
are specified in [05-doctype-schemas.md](05-doctype-schemas.md) §7.

**N8 — Version numbering is inconsistent.** The app is `0.0.1`; refactor.md prescribes
`patches/v2_0/` and "remove in 2.0". See decision **D1**.

**N9 — Blast radius is one site.** Only `drwash` has the app installed in this bench, so
key rotation, Error Log purge and end-to-end testing all target that site. Forks and
Marketplace installs are the wider audience and are only reachable via the advisory.

## 4. Errors and gaps in refactor.md's target design

These are corrections to apply while executing; each is already folded into the phase
docs.

**E1 — `frappe.cache().lock(...)` is not a Frappe API.** `RedisWrapper`
(`frappe/utils/redis_wrapper.py:37`) defines no `lock`; the call would resolve to
`redis.Redis.lock` inherited from redis-py 7.1.0, which does **not** apply Frappe's
site prefix (`make_key`). On this bench six sites share one Redis, so an unprefixed
lock name collides across sites. Use:

```python
lock_key = frappe.cache.make_key(f"fcm:token:lock:{project}")
with frappe.cache.lock(lock_key, timeout=30, blocking_timeout=10):
    ...
```

`frappe.cache.set_value` / `get_value` already prefix, so the token cache key itself
needs no `make_key`. (`frappe.utils.synchronization.filelock` is the file-based
alternative but is per-node, not cross-worker across machines — do not use it here.)

**E2 — `allow_guest=False` + HMAC headers cannot both hold for an external app.** A
request carrying only `X-Push-Key`/`X-Push-Signature` is Guest and is rejected by
`@frappe.whitelist(allow_guest=False)` *before* `@require_api_client` ever runs. Two
consistent options:

- **(a) Recommended.** The calling app authenticates as a normal Frappe API user
  (`Authorization: token <key>:<secret>` on a dedicated user holding only the
  `Push API Client` role) and the HMAC signature carries **scoping** (which project,
  which audience, replay protection). No `allow_guest`, no new auth surface.
- **(b)** Register an `auth_hooks` entry that resolves `X-Push-Key` + HMAC and calls
  `frappe.set_user()`. Verified mechanism (`frappe/auth.py:742-744`), but it runs on
  *every* request in the site and is a much larger security surface.

Decision **D2**.

**E3 — Two child doctypes are missing from the inventory.** `Push Audience.users`
(Table MultiSelect → User) needs `Push Audience User`; `Push API Client.allowed_audiences`
(Table MultiSelect → Push Audience) needs `Push API Client Audience`. Both are specified
in [05-doctype-schemas.md](05-doctype-schemas.md).

**E4 — The `Small Text` rationale is wrong.** In MariaDB, Frappe maps `Small Text` →
`text` (`frappe/database/mariadb/database.py:178`), which is no more indexable than
`Long Text`. The design is still right — `token_hash` (Data) carries the unique index —
but the stated reason must be corrected so nobody later tries to index `token`.

**E5 — `unique on (device_id, fcm_project) where device_id is set` is not expressible.**
MariaDB has no partial indexes. A composite UNIQUE does tolerate duplicate rows when a
column is `NULL`, but Frappe defaults a `Data` field to `''`, not `NULL` — so every
anonymous device would collide on `('', 'default')`. Fix: in `before_insert`/`validate`,
coerce blank `device_id` to `None`, and document that the uniqueness guarantee only
applies to rows that supply one.

**E6 — Campaign counters on a submitted document.** `Push Campaign` is submittable, so
`sent_count` / `failed_count` / `invalid_count` / `status` / `started_at` /
`completed_at` must be `allow_on_submit: 1` and written with `db_set` /
`frappe.db.set_value`, never `doc.save()`.

**E7 — `frappe.db.bulk_insert` needs full rows.** Signature is
`bulk_insert(doctype, fields, values, ignore_duplicates=False, chunk_size=1000)`
(`frappe/database/database.py:1479`) and it writes raw rows — the caller must supply
`name`, `creation`, `modified`, `owner`, `modified_by` itself
(`name=frappe.generate_hash(length=10)`).

**E8 — Do not follow §9's Python guidance.** Keep `requires-python = ">=3.14,<3.15"`;
keep the CI matrix on 3.14. See P1.

**E9 — Phase 0's acceptance grep needs a companion.** `grep -rn "whitelist"` over the
package will pass while `README.md:94` still documents the removed endpoint; the README
line is part of Phase 0 (N3).

**E10 — No project-selection rule for the legacy notification path.** Once `FCM Project`
is multi-row, `on_notification_log` must decide which project a given Notification Log
belongs to. Rule: use the project flagged `is_default`; allow override through a new
`fcm_project_resolver` hook (dotted path, allowlisted like `fcm_audience_resolvers`).
Single-project sites keep working with zero configuration.

**E11 — "Block-with-timeout" throttling can park RQ workers.** That is the failure mode
C2 exists to prevent. Bound it: block at most `throttle_max_block_sec` (default 5s), then
stop the batch, mark it `Pending`, and re-enqueue with backoff rather than sleeping in
the worker.

**E12 — A module-level credential cache cannot be invalidated by a doc hook.** §6.1
proposes caching the parsed service account in a module-level dict "invalidated by an
`on_update` hook on FCM Project". The hook runs in whichever process saved the document;
every other RQ worker process keeps its stale copy until restart. Fix: bump a Redis
counter `fcm:cred_version:{project}` in `on_update`, store the version alongside the
cached credentials, and re-read when the counter moves (one cheap Redis GET per send,
no re-decrypt).

**E13 — Untracked in-bench consumer.** `dr_wash/utils/push.py` should stop duplicating
device resolution once `api/v1.send` exists. Not in refactor.md's scope; scheduled as an
optional task in Phase 4 (decision **D3**).

**E14 — `strip_html_tags` does not handle entities.** refactor.md §6.3 prescribes it as
the `cleanhtml` replacement "because it handles entities" (C12). It does not:
`frappe/utils/__init__.py:442` is a single tag-stripping regex, so `&amp;` and `&nbsp;`
survive it intact — verified by test. The fix needs two steps,
`unescape_html(strip_html_tags(text))`, plus normalising the resulting `\xa0` to a space.
Swapping `cleanhtml` for `strip_html_tags` alone would have left C12 open while looking
fixed.

## 5. Decisions

| ID | Question | Resolution |
|----|----------|------------|
| **D1** | Version/patch-directory naming (app was at 0.0.1, refactor.md says `v2_0`) | **Settled.** `0.1.0` shipped for the hotfix; `1.0.0` at the end of Milestone 3; patches under `patches/v1_0/`; shims documented as "removed in 2.0" |
| **D2** | External API auth model (E2) | **Settled.** Option (a): dedicated Frappe API user + `Push API Client` HMAC scoping. No `auth_hooks`. Milestone 3 |
| **D3** | Update `dr_wash/utils/push.py` in this pass? | **Settled — yes, Milestone 1.** No longer optional: with `User Device` deleted the current implementation would not resolve at all |
| **D4** | Hotfix branch base | **Settled.** Branched off `version-16`, tagged `v0.1.0`; merge forward into `main` outstanding |
| **D5** | Is multi-Firebase-project actually needed now? | **Settled.** Build `FCM Project` as a table (cheap, unblocks X5), ship one `is_default` row, no per-client routing UI until a second client exists |
| **D6** | Marketplace/advisory handling | Human action — see [01-phase-0-security-hotfix.md](01-phase-0-security-hotfix.md) §4. Still outstanding |
| **D7** | Firebase project for end-to-end testing | **Assumed unavailable.** Everything is mock-tested; one manual smoke test against a real project is required before `1.0.0`. Revisit if credentials appear |

## 6. Greenfield amendment (supersedes the six-phase structure)

Verified on `drwash` after Phase 0 shipped:

- Every credential field on `FCM Notification Settings` is empty — `project_id`,
  `client_email`, `private_key`, `private_key_id`, all cert URLs, `universe_domain`,
  `access_token`, `expiration_time`. The only non-empty fields are the two defaults the
  `v0_0_1` patch backfilled.
- `tabUser Device` has 0 rows.
- The user confirms no active project is running this app.

**The app has never sent a notification on this bench.** Three consequences:

1. **The disclosure is not an incident here.** No key was ever configured, so there is
   nothing to rotate on this bench. It remains a real vulnerability for the 16 public
   forks and the Marketplace listing, so `v0.1.0` and the advisory still matter — but
   there is no live compromise to contain. **Caveat:** this covers only this bench. A
   deployment elsewhere with real credentials would still need the full §4 rotation.
2. **N1 is not an outage.** It is a latent bug in a path nobody reaches, and it
   disappears when `Push Device` replaces `User Device`. Off the critical path.
3. **Migrations stop constraining the design.** Keeping `User Device` alive, staging
   deprecations and sequencing three patches all existed to protect data and callers that
   do not exist. Build the target schema directly; add a best-effort migration for fork
   users at release time and do not let it shape anything.

Phase gating collapses with it — "ship, tag, verify on a live site, then start the next
phase" bounded a blast radius that isn't there. Six phases become three milestones:

| Milestone | Scope | Was |
|-----------|-------|-----|
| **1 — Core send path** | `fcm/` package, `FCM Project`, `Push Device`, `api/v1` register/unregister/send, delete `User Device` + iOS conversion, rewrite `dr_wash/utils/push.py` | Phases 1, 2, part of 4 |
| **2 — Bulk engine** | audience, dispatch, throttle, `Push Campaign`/`Push Batch`/`Push Delivery Log` | Phase 3 |
| **3 — Release** | topics, scheduled campaigns, `Push API Client` + HMAC, webhooks, courtesy migration patch, README + migration guide, CI gate, `1.0.0` | Phases 4–6 |

**What does not relax:** the security constraints from Phase 0 (no whitelist outside
`api/v1.py`, no `allow_guest`, no token or response-body logging), enforced by
`test_security.py` and a CI grep gate. Those are permanent, not phase-scoped.

**What this costs:** with no credentials anywhere, nothing can be verified end-to-end.
Mocks throughout, no test touches the network, and one manual smoke test against a real
Firebase project gates `1.0.0` (D7).
