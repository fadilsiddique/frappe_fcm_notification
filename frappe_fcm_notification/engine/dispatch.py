# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Delivery: transactional sends and campaign fan-out.

Two paths share one transport. Transactional sends go straight out on the short queue.
Campaigns are split into Push Batch rows first, which is what makes them resumable: a
worker that dies takes one batch with it, and the batch retries on its own.

Throughput on the token path is our own concurrency - FCM HTTP v1 has no multicast.
"""

import json
import random

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from frappe_fcm_notification.engine.audience import Strategy, resolve
from frappe_fcm_notification.engine.throttle import ThrottleTimeout, consume
from frappe_fcm_notification.fcm.client import FCMClient
from frappe_fcm_notification.fcm.credentials import resolve_project
from frappe_fcm_notification.fcm.errors import Action, backoff_seconds
from frappe_fcm_notification.fcm.payload import build_message
from frappe_fcm_notification.frappe_fcm_notification.doctype.push_device.push_device import (
	deactivate_tokens,
	get_active_tokens,
	hash_token,
)

# Anything larger belongs in a campaign, which is resumable and rate limited.
MAX_TRANSACTIONAL_TOKENS = 50

# Batch size. 500 tokens at 15 threads is roughly three seconds of work - small enough
# to retry cheaply, large enough that batch bookkeeping stays a rounding error.
BATCH_SIZE = 500
MAX_BATCH_RETRIES = 5

# A campaign that has been Running for longer than this is not coming back on its own.
STALE_CAMPAIGN_HOURS = 24

LOGGER = "frappe_fcm_notification"


# ---------------------------------------------------------------- transactional


def on_notification_log(doc, method=None):
	"""doc_events: Notification Log after_insert."""
	if not doc.for_user:
		return

	project = resolve_project_for(doc)
	tokens = get_active_tokens(user=doc.for_user, fcm_project=project)
	if not tokens:
		return

	frappe.enqueue(
		send_transactional,
		queue="short",
		# The job must not start before the Notification Log row is committed.
		enqueue_after_commit=True,
		project=project,
		tokens=tokens[:MAX_TRANSACTIONAL_TOKENS],
		# Newer v16 fills title/description from the Notification's "Notification Title /
		# Message" and leaves subject/email_content as whatever the hidden fields hold.
		title=doc.get("title") or doc.subject,
		body=doc.get("description") or doc.email_content,
		data={
			"doctype": doc.document_type or "",
			"docname": str(doc.document_name or ""),
			"notification_log": doc.name,
		},
	)


def resolve_project_for(doc) -> str:
	"""Which Firebase project should serve this notification.

	Defaults to the project flagged is_default. Sites running more than one app can
	override through the fcm_project_resolver hook - a hook, not a doctype field, so the
	dotted path can only come from installed app code.
	"""
	for method in frappe.get_hooks("fcm_project_resolver") or []:
		project = frappe.call(frappe.get_attr(method), doc=doc)
		if project:
			return project

	return resolve_project()


def send_transactional(
	tokens: list[str],
	title: str,
	body: str,
	data: dict | None = None,
	project: str | None = None,
) -> dict:
	"""Send one message to a handful of tokens and retire the dead ones.

	Returns counts only - never the tokens, which are credentials.
	"""
	tokens = [t for t in (tokens or []) if t][:MAX_TRANSACTIONAL_TOKENS]
	if not tokens:
		return {"sent": 0, "failed": 0, "invalid": 0}

	client = FCMClient(project)
	messages = [
		build_message(token=token, title=title, body=body, data=data, project_config=client.config)
		for token in tokens
	]

	outcome = _classify_results(tokens, client.send_many(messages))

	if outcome["dead"]:
		deactivate_tokens(outcome["dead"], reason=outcome["reason"])

	if outcome["failed"] or outcome["dead"]:
		frappe.logger(LOGGER).warning(
			f"push: sent={outcome['sent']} failed={outcome['failed']} "
			f"invalid={len(outcome['dead'])} project={client.project}"
		)

	return {
		"sent": outcome["sent"],
		"failed": outcome["failed"],
		"invalid": len(outcome["dead"]),
	}


# -------------------------------------------------------------------- campaigns


def dispatch_campaign(campaign_name: str) -> None:
	"""Coordinator. Resolves the audience and fans work out into Push Batch rows."""
	campaign = frappe.get_doc("Push Campaign", campaign_name)

	if campaign.status not in ("Queued", "Running"):
		return

	campaign.db_set({"status": "Running", "started_at": now_datetime()}, update_modified=False)

	try:
		audience = frappe.get_doc("Push Audience", campaign.audience)
		strategy, target = resolve(audience, campaign.fcm_project)
		campaign.db_set("strategy", strategy.value, update_modified=False)

		if strategy == Strategy.TOPIC:
			_send_to_topic(campaign, target)
		else:
			_fan_out_batches(campaign, target)

	except Exception as e:
		campaign.db_set(
			{"status": "Failed", "error_message": str(e)[:500], "completed_at": now_datetime()},
			update_modified=False,
		)
		frappe.logger(LOGGER).error(f"campaign {campaign_name} failed: {e.__class__.__name__}")
		raise


def _send_to_topic(campaign, topic: str) -> None:
	"""A topic broadcast is one API call no matter how large the audience is."""
	client = FCMClient(campaign.fcm_project)
	message = build_message(
		topic=topic,
		title=campaign.title,
		body=campaign.body,
		data=_campaign_data(campaign),
		image=campaign.image_url,
		project_config=client.config,
	)

	action, code, _retry_after = client.send(message)
	succeeded = action == Action.OK

	campaign.db_set(
		{
			"status": "Completed" if succeeded else "Failed",
			"total_recipients": 1,
			"sent_count": 1 if succeeded else 0,
			"failed_count": 0 if succeeded else 1,
			"completed_at": now_datetime(),
			"error_message": "" if succeeded else f"topic send failed: {code}",
		},
		update_modified=False,
	)

	_notify_completion(campaign.name)


def _fan_out_batches(campaign, tokens) -> None:
	"""Consume the token generator into batches, enqueuing each as it is created.

	total_recipients grows as batches are created because a generator has no length, and
	counting it up front would mean materialising the audience.
	"""
	index = 0
	total = 0
	chunk: list[str] = []

	for token in tokens:
		chunk.append(token)
		if len(chunk) < BATCH_SIZE:
			continue

		total += _create_batch(campaign, index, chunk)
		index += 1
		chunk = []

		if _is_cancelled(campaign.name):
			break

	if chunk and not _is_cancelled(campaign.name):
		total += _create_batch(campaign, index, chunk)
		index += 1

	campaign.db_set("total_recipients", total, update_modified=False)

	if not index:
		campaign.db_set(
			{"status": "Completed", "completed_at": now_datetime()}, update_modified=False
		)
		_notify_completion(campaign.name)


def _create_batch(campaign, index: int, tokens: list[str]) -> int:
	batch = frappe.get_doc(
		{
			"doctype": "Push Batch",
			"campaign": campaign.name,
			"batch_index": index,
			"tokens": json.dumps(tokens),
			"status": "Pending",
		}
	).insert(ignore_permissions=True)

	frappe.enqueue(
		process_batch,
		queue="long",
		timeout=1800,
		enqueue_after_commit=True,
		deduplicate=True,
		job_id=f"push-batch-{batch.name}",
		batch_name=batch.name,
	)

	return len(tokens)


def process_batch(batch_name: str) -> None:
	"""Worker. Sends one batch, retires dead tokens and books the results once."""
	batch = frappe.get_doc("Push Batch", batch_name)

	if batch.status in ("Done", "Dead"):
		return

	campaign = frappe.get_doc("Push Campaign", batch.campaign)
	if campaign.status == "Cancelled":
		batch.db_set("status", "Dead", update_modified=False)
		return

	tokens = json.loads(batch.tokens or "[]")
	if not tokens:
		batch.db_set("status", "Done", update_modified=False)
		_finalise_if_complete(campaign.name)
		return

	batch.db_set("status", "Running", update_modified=False)
	client = FCMClient(campaign.fcm_project)

	try:
		consume(
			campaign.fcm_project,
			count=len(tokens),
			max_block_sec=cint(client.config.throttle_max_block_sec) or 5,
		)
	except ThrottleTimeout as e:
		# Requeue rather than hold a worker hostage to the rate limit.
		_schedule_retry(batch, str(e))
		return

	messages = [
		build_message(
			token=token,
			title=campaign.title,
			body=campaign.body,
			data=_campaign_data(campaign),
			image=campaign.image_url,
			project_config=client.config,
		)
		for token in tokens
	]

	outcome = _classify_results(tokens, client.send_many(messages))

	if outcome["dead"]:
		deactivate_tokens(outcome["dead"], reason=outcome["reason"])

	_write_delivery_logs(campaign, batch, outcome, client.config)

	_book_counts(campaign.name, batch, outcome)

	if outcome["retry"] and cint(batch.retry_count) < MAX_BATCH_RETRIES:
		# Retry only what has not landed yet.
		batch.db_set("tokens", json.dumps(outcome["retry"]), update_modified=False)
		_schedule_retry(batch, f"retrying {len(outcome['retry'])} tokens: {outcome['reason']}")
		return

	batch.db_set(
		{
			"status": "Dead" if outcome["retry"] else "Done",
			"last_error": outcome["reason"] if outcome["retry"] else "",
		},
		update_modified=False,
	)

	_finalise_if_complete(campaign.name)


def run_scheduled_campaigns() -> None:
	"""Cron. Starts due campaigns, retries stalled batches, buries stale campaigns."""
	_start_due_campaigns()
	_retry_due_batches()
	_fail_stale_campaigns()


# --------------------------------------------------------------------- internals


def _classify_results(tokens: list[str], results) -> dict:
	sent = failed = 0
	dead: list[str] = []
	retry: list[str] = []
	failures: list[tuple[str, str]] = []
	reason = ""

	for token, (action, code, _retry_after) in zip(tokens, results, strict=True):
		if action == Action.OK:
			sent += 1
		elif action == Action.DROP_TOKEN:
			dead.append(token)
			failures.append((token, code))
			reason = reason or code
		elif action == Action.RETRY:
			retry.append(token)
			reason = reason or code
		else:
			failed += 1
			failures.append((token, code))
			reason = reason or code

	return {
		"sent": sent,
		"failed": failed,
		"dead": dead,
		"retry": retry,
		"failures": failures,
		"reason": reason or "UNREGISTERED",
		"tokens": tokens,
	}


def _campaign_data(campaign) -> dict:
	data = {}

	if campaign.data_payload:
		payload = campaign.data_payload
		data.update(json.loads(payload) if isinstance(payload, str) else payload)

	if campaign.deep_link:
		data["deep_link"] = campaign.deep_link

	data["campaign"] = campaign.name
	return data


def _is_cancelled(campaign_name: str) -> bool:
	return frappe.db.get_value("Push Campaign", campaign_name, "status") == "Cancelled"


def _schedule_retry(batch, error: str) -> None:
	attempt = cint(batch.retry_count) + 1
	delay = backoff_seconds(attempt)

	batch.db_set(
		{
			"status": "Pending",
			"retry_count": attempt,
			"next_retry_at": add_to_date(now_datetime(), seconds=int(delay)),
			"last_error": error[:500],
		},
		update_modified=False,
	)


def _book_counts(campaign_name: str, batch, outcome: dict) -> None:
	"""Incremental UPDATE, not read-modify-write: batches finish concurrently."""
	invalid = len(outcome["dead"])

	batch.db_set(
		{
			"sent_count": cint(batch.sent_count) + outcome["sent"],
			"failed_count": cint(batch.failed_count) + outcome["failed"],
			"invalid_count": cint(batch.invalid_count) + invalid,
		},
		update_modified=False,
	)

	table = frappe.qb.DocType("Push Campaign")
	(
		frappe.qb.update(table)
		.set(table.sent_count, table.sent_count + outcome["sent"])
		.set(table.failed_count, table.failed_count + outcome["failed"])
		.set(table.invalid_count, table.invalid_count + invalid)
		.where(table.name == campaign_name)
	).run()


def _write_delivery_logs(campaign, batch, outcome: dict, config) -> None:
	"""Failures in full, successes sampled.

	One row per recipient at 100k recipients is a table nobody can query and a write
	bottleneck on the send path.
	"""
	rows = list(outcome["failures"])

	sample_rate = (cint(config.success_sample_rate) or 0) / 100
	if sample_rate > 0 and outcome["sent"]:
		delivered = set(outcome["tokens"]) - set(outcome["dead"]) - set(outcome["retry"])
		rows.extend((token, "") for token in delivered if random.random() < sample_rate)

	if not rows:
		return

	devices = _device_names([token for token, _ in rows])
	dead = set(outcome["dead"])
	timestamp = now_datetime()
	user = frappe.session.user or "Administrator"

	values = []
	for token, code in rows:
		if code:
			status = "Invalid" if token in dead else "Failed"
		else:
			status = "Sent"

		values.append(
			(
				frappe.generate_hash(length=10),
				timestamp,
				timestamp,
				user,
				user,
				campaign.name,
				batch.name,
				devices.get(hash_token(token)),
				status,
				code[:140],
				"",
				timestamp,
			)
		)

	frappe.db.bulk_insert(
		"Push Delivery Log",
		fields=[
			"name",
			"creation",
			"modified",
			"owner",
			"modified_by",
			"campaign",
			"batch",
			"device",
			"status",
			"fcm_error_code",
			"error_message",
			"sent_at",
		],
		values=values,
		ignore_duplicates=True,
	)


def _device_names(tokens: list[str]) -> dict[str, str]:
	hashes = [hash_token(token) for token in tokens]
	rows = frappe.get_all(
		"Push Device", filters={"token_hash": ("in", hashes)}, fields=["name", "token_hash"]
	)
	return {row.token_hash: row.name for row in rows}


def _finalise_if_complete(campaign_name: str) -> None:
	pending = frappe.db.count(
		"Push Batch", {"campaign": campaign_name, "status": ("in", ("Pending", "Running"))}
	)
	if pending:
		return

	campaign = frappe.get_doc("Push Campaign", campaign_name)
	if campaign.status in ("Completed", "Failed", "Cancelled"):
		return

	dead = frappe.db.count("Push Batch", {"campaign": campaign_name, "status": "Dead"})
	total = frappe.db.count("Push Batch", {"campaign": campaign_name})

	campaign.db_set(
		{
			"status": "Failed" if dead and dead == total else "Completed",
			"completed_at": now_datetime(),
		},
		update_modified=False,
	)

	_notify_completion(campaign_name)


def _notify_completion(campaign_name: str) -> None:
	from frappe_fcm_notification.engine.webhooks import enqueue_completion_webhook

	enqueue_completion_webhook(campaign_name)


def _start_due_campaigns() -> None:
	due = frappe.get_all(
		"Push Campaign",
		filters={
			"status": "Queued",
			"docstatus": 1,
			"scheduled_at": ("<=", now_datetime()),
		},
		pluck="name",
		limit_page_length=100,
	)

	for name in due:
		if not _claim_campaign(name):
			# Another cron run got there first.
			continue

		frappe.enqueue(
			dispatch_campaign,
			queue="long",
			timeout=3600,
			deduplicate=True,
			job_id=f"push-campaign-{name}",
			campaign_name=name,
		)


def _claim_campaign(name: str) -> bool:
	"""Atomic claim so two overlapping cron runs cannot dispatch the same campaign.

	The conditional UPDATE is the lock: the loser's statement matches zero rows because
	the winner already moved the status. Affected-row count comes from the cursor -
	the query builder's run() returns the result set, which is empty for an UPDATE and
	would report every claim as successful.
	"""
	frappe.db.sql(
		"""
		update `tabPush Campaign`
		set status = 'Running', started_at = %s
		where name = %s and status = 'Queued'
		""",
		(now_datetime(), name),
	)
	claimed = bool(frappe.db._cursor.rowcount)

	# The claim only excludes the other runner once it is visible to it.
	frappe.db.commit()
	return claimed


def _retry_due_batches() -> None:
	batches = frappe.get_all(
		"Push Batch",
		filters={"status": "Pending", "next_retry_at": ("<=", now_datetime())},
		pluck="name",
		limit_page_length=500,
	)

	for name in batches:
		frappe.enqueue(
			process_batch,
			queue="long",
			timeout=1800,
			deduplicate=True,
			job_id=f"push-batch-{name}",
			batch_name=name,
		)


def _fail_stale_campaigns() -> None:
	"""A campaign stuck Running past the cutoff is not recovering.

	Better to bury it with a reason than to fire a day-old notification after an outage.
	"""
	cutoff = add_to_date(now_datetime(), hours=-STALE_CAMPAIGN_HOURS)
	stale = frappe.get_all(
		"Push Campaign",
		filters={"status": "Running", "started_at": ("<", cutoff)},
		pluck="name",
		limit_page_length=100,
	)

	for name in stale:
		frappe.db.set_value(
			"Push Campaign",
			name,
			{
				"status": "Failed",
				"completed_at": now_datetime(),
				"error_message": f"abandoned after {STALE_CAMPAIGN_HOURS}h without completing",
			},
			update_modified=False,
		)
