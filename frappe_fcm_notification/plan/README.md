# frappe_fcm_notification — refactor plan set

Working plans derived from [refactor.md](../refactor.md), reconciled against the code on
disk and the running bench (Frappe 16.18.2, Python 3.14.0).

`refactor.md` stays the architectural intent. These files are the executable plan; where
`refactor.md` is stale, wrong, or under-specified, **these files win**, and the divergence
is recorded in [00-analysis.md](00-analysis.md).

## Status

| | |
|---|---|
| **Phase 0 (security hotfix)** | **Shipped** — `v0.1.0`, branch `hotfix/credential-disclosure` |
| **Milestone 1 (core send path)** | in progress |
| Milestone 2 (bulk engine) | not started |
| Milestone 3 (release) | not started |

## Why three milestones and not six phases

The original six-phase plan was shaped by constraints that turned out not to exist on
this bench: `FCM Notification Settings` has never been configured (every credential field
empty) and `tabUser Device` has zero rows, so there is no production data to migrate and
no live caller to protect. Phase gating, staged deprecation and three migration patches
all existed to bound blast radius that isn't there.

What survives unchanged: the security discipline (Phase 0's guarantees are permanent
constraints, enforced in CI), the doctype schemas, and a best-effort migration for the
16 public forks — added at release time, not allowed to shape the design.

See [00-analysis.md](00-analysis.md) §6 for the full amendment.

## Read in this order

| File | What it is |
|------|------------|
| [00-analysis.md](00-analysis.md) | refactor.md vs. real code — per-defect verdicts, new findings, design errors, decisions, greenfield amendment |
| [01-phase-0-security-hotfix.md](01-phase-0-security-hotfix.md) | **Done.** Credential disclosure fix, shipped as `v0.1.0` |
| [02-milestone-1-core.md](02-milestone-1-core.md) | `fcm/` package, FCM Project, Push Device, `api/v1` register/send, legacy removal |
| [03-milestone-2-engine.md](03-milestone-2-engine.md) | Audience, dispatch, throttle, Campaign/Batch/Delivery Log |
| [04-milestone-3-release.md](04-milestone-3-release.md) | Topics, scheduling, Push API Client, webhooks, migration, docs, CI, 1.0.0 |
| [05-doctype-schemas.md](05-doctype-schemas.md) | Corrected doctype schemas — single source of truth for all milestones |

## Standing constraints (permanent — from refactor.md §12, hardened by Phase 0)

- **Never** `@frappe.whitelist()` outside `api/v1.py`.
- **Never** `allow_guest=True` anywhere in this app.
- **Never** log tokens, access tokens, credential objects, or FCM response bodies.
- **Never** `frappe.db.commit()` inside a `doc_events` hook.
- **Never** pass Document objects to `enqueue`.
- **Never** materialise an audience as a list.
- Always `timeout=` on outbound HTTP.
- Always `enqueue_after_commit=True` for document-triggered jobs.
- Keep it lean — no analytics dashboards, A/B testing, or template designer in this pass.

The first three are enforced by `test_security.py` and a CI grep gate. They are the
regression guard for the actual vulnerability and must not be skippable.

## Verification posture

With no Firebase credentials configured anywhere on this bench, **everything is
mock-tested**. No test may touch the network. A single manual smoke test against a real
Firebase project is required before `1.0.0` ships — that is the one gap mocks cannot close.
