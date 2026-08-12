# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Transactional delivery.

One job per user rather than one per device: the old code enqueued a job per token,
each of which re-read the settings and re-decrypted the private key.
"""

import frappe

from frappe_fcm_notification.fcm.client import FCMClient
from frappe_fcm_notification.fcm.credentials import resolve_project
from frappe_fcm_notification.fcm.errors import Action
from frappe_fcm_notification.fcm.payload import build_message
from frappe_fcm_notification.frappe_fcm_notification.doctype.push_device.push_device import (
	deactivate_tokens,
	get_active_tokens,
)

# Anything larger belongs in a campaign, which is resumable and rate limited.
MAX_TRANSACTIONAL_TOKENS = 50


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
		title=doc.subject,
		body=doc.email_content,
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
		build_message(
			token=token, title=title, body=body, data=data, project_config=client.config
		)
		for token in tokens
	]

	results = client.send_many(messages)

	sent = failed = 0
	dead: list[str] = []
	reasons: dict[str, str] = {}

	for token, (action, code, _retry_after) in zip(tokens, results, strict=True):
		if action == Action.OK:
			sent += 1
		elif action == Action.DROP_TOKEN:
			dead.append(token)
			reasons[code] = code
		else:
			failed += 1

	if dead:
		deactivate_tokens(dead, reason=next(iter(reasons), "UNREGISTERED"))

	if failed or dead:
		frappe.logger("frappe_fcm_notification").warning(
			f"push: sent={sent} failed={failed} invalid={len(dead)} project={client.project}"
		)

	return {"sent": sent, "failed": failed, "invalid": len(dead)}
