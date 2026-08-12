# Milestone 2 — Bulk engine

**Branch:** `feat/bulk-engine`. **Gate:** a 10 000-token mocked campaign completes, is
resumable after a worker kill, and the counters are accurate.

Was Phase 3. Fixes X1, X3, X4, X7, C9. Doctypes per
[05-doctype-schemas.md](05-doctype-schemas.md) §3–§5.

## 1. `engine/audience.py`

```python
class Strategy(StrEnum): TOPIC / TOKEN
def resolve(audience_doc, project) -> tuple[Strategy, Iterator[str] | str]
```

- **The token side must be a generator** — a 200k-row `frappe.get_all` OOMs the worker.
  Page with `limit_start` / `limit_page_length=1000`, ordered by `name`, always filtering
  `is_active = 1` and `fcm_project = project`.
- Auto-selection: `All` and anything mapping cleanly onto a maintained topic → `TOPIC`;
  everything else → `TOKEN`. `strategy_hint` overrides.
- **`Method` audiences are the injection surface.** `resolver_method` must be present in
  the `fcm_audience_resolvers` hook allowlist. Never `frappe.get_attr` a user-supplied
  dotted path — that is arbitrary code execution for anyone with write access to one
  doctype.
- `Report` audiences run with the campaign owner's permissions and must yield a `user` or
  `token` column.

## 2. `engine/dispatch.py`

```python
def dispatch_campaign(campaign_name)      # coordinator, queue="long"
def process_batch(batch_name)             # worker,      queue="long"
def send_transactional(project, tokens, title, body, data=None)   # <=50, queue="short"
def run_scheduled_campaigns()             # cron, Milestone 3
```

**Coordinator:** resolve; `TOPIC` → one send, `Completed`, return. `TOKEN` → consume the
generator in chunks of 500 into `Push Batch` rows, enqueuing each with
`enqueue_after_commit=True, deduplicate=True, job_id=f"push-batch-{name}"` (all verified
present in `frappe/utils/background_jobs.py:76-93`; note `enqueue_after_commit` makes
`enqueue` return `None`). Increment `total_recipients` as batches are created — the
generator has no length. Re-check `status != "Cancelled"` between chunks.

**Worker:** one `AuthorizedSession` per batch (X2), `ThreadPoolExecutor(max_workers=15)`
(X3), throttle check per dispatch window (X4), every response through
`fcm.errors.classify`. One commit at the end: bulk-deactivate `DROP_TOKEN` devices, bulk
insert delivery-log rows, update counters with incremental `UPDATE` (**E6:** the campaign
is submitted — counters need `allow_on_submit` and `db_set`, never `save()`; and
`SET sent_count = sent_count + %s` avoids read-modify-write drift). Retry RETRY-classified
failures with backoff up to `max_retries = 5`, retrying only unsent tokens, then `Dead`.

## 3. `engine/throttle.py`

Redis token bucket at `frappe.cache.make_key(f"fcm:throttle:{project}")`, refilled at
`FCM Project.rate_limit_per_second`. **E11:** block at most `throttle_max_block_sec`
(default 5s), then leave the batch `Pending` and re-enqueue with backoff. Sleeping
indefinitely in an RQ worker is the exact failure C2 exists to prevent.

## 4. Logging policy (C9)

`Push Delivery Log` records failures and invalids in full plus a ~1% success sample
(`FCM Project.success_sample_rate`). Written with `frappe.db.bulk_insert` — **E7: supply
`name`, `creation`, `modified`, `owner`, `modified_by` yourself**
(`frappe/database/database.py:1479`), `name = frappe.generate_hash(length=10)`. Register
`default_log_clearing_doctypes = {"Push Delivery Log": 30}`. Never `frappe.log_error` per
message; aggregate errors live on `Push Batch.last_error`.

## 5. Sizing

500 tokens × 15 concurrent × ~80 ms ≈ 3 s per batch. 100k recipients = 200 batches;
8 workers ≈ 75 s. All of it our own concurrency — there is no multicast.

## 6. Acceptance

- 1200 mocked tokens → exactly 3 batches.
- A mocked 429 requeues its batch and honours `Retry-After`; the campaign is not failed.
- A mocked `UNREGISTERED` deactivates exactly the offending device.
- Worker killed mid-campaign → survivors complete, killed batch retries, final counters
  equal the recipient count.
- Cancelling stops new batch creation within one chunk.
- A 10 000-token campaign writes ≤ ~150 delivery-log rows, not 10 000.
- `test_audience.py` (generators, allowlist enforcement), `test_dispatch.py` (batching,
  429, `UNREGISTERED`, cancel).

## 7. Risks

| Risk | Mitigation |
|------|------------|
| Long batches exceed the RQ job timeout | Explicit `timeout` on `enqueue`, sized so it cannot be hit |
| A resolver raises mid-generation | Coordinator catches per chunk, marks the campaign `Failed` with the reason, keeps created batches |
| Throttle starves under many workers | Bucket is shared through Redis and per project, so the limit holds across workers |
