# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""The only whitelisted surface in this app.

Every function here is reachable over HTTP, so each one states who may call it and
validates what it is given. Nothing in this file may ever use allow_guest: an
unauthenticated endpoint is what let anonymous callers force OAuth refreshes, database
writes and outbound Google calls in 0.0.1.
"""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from frappe_fcm_notification.api.auth import assert_can_use, require_api_client
from frappe_fcm_notification.engine.dispatch import MAX_TRANSACTIONAL_TOKENS, send_transactional
from frappe_fcm_notification.frappe_fcm_notification.doctype.push_device.push_device import (
	deactivate_tokens,
	hash_token,
	upsert_device,
)


@frappe.whitelist()
@rate_limit(key="token", limit=20, seconds=60 * 60, ip_based=False)
def register_device(
	token: str,
	platform: str | None = None,
	fcm_project: str | None = None,
	device_id: str | None = None,
	device_name: str | None = None,
	app_version: str | None = None,
	locale: str | None = None,
):
	"""Register or refresh the calling user's device.

	The device is always bound to the session user; a caller cannot register a token
	against somebody else.
	"""
	if not token:
		frappe.throw(_("token is required."))

	name = upsert_device(
		token=token,
		platform=platform,
		user=frappe.session.user,
		fcm_project=fcm_project,
		device_id=device_id,
		device_name=device_name,
		app_version=app_version,
		locale=locale,
	)

	return {"device": name}


@frappe.whitelist()
def unregister_device(token: str):
	"""Retire one of the calling user's devices.

	Scoped to the session user so that knowing a token is not enough to silence someone
	else's device. System Managers may retire any device.
	"""
	if not token:
		frappe.throw(_("token is required."))

	owner = frappe.db.get_value("Push Device", {"token_hash": hash_token(token)}, "user")
	if owner and owner != frappe.session.user and "System Manager" not in frappe.get_roles():
		raise frappe.PermissionError

	deactivate_tokens([token], reason="UNREGISTERED_BY_CLIENT")
	return {"ok": True}


@frappe.whitelist()
def send(
	tokens,
	title: str,
	body: str,
	data: dict | None = None,
	project: str | None = None,
):
	"""Send a transactional push to specific tokens.

	Restricted to System Manager until Push API Client lands: an open endpoint that
	pushes arbitrary content to arbitrary tokens is exactly the hole that was closed in
	0.1.0.
	"""
	frappe.only_for("System Manager")

	tokens = frappe.parse_json(tokens) if isinstance(tokens, str) else tokens
	if isinstance(tokens, str):
		tokens = [tokens]
	if not tokens:
		frappe.throw(_("At least one token is required."))

	if len(tokens) > MAX_TRANSACTIONAL_TOKENS:
		frappe.throw(
			_("send() accepts at most {0} tokens. Use a campaign for larger audiences.").format(
				MAX_TRANSACTIONAL_TOKENS
			)
		)

	if isinstance(data, str):
		data = frappe.parse_json(data)

	return send_transactional(
		tokens=tokens, title=title, body=body, data=data, project=project
	)


@frappe.whitelist()
@require_api_client
def broadcast(
	project: str,
	audience: str,
	title: str,
	body: str,
	data: dict | None = None,
	scheduled_at: str | None = None,
	idempotency_key: str | None = None,
	callback_url: str | None = None,
	api_client=None,
):
	"""Queue a campaign. Returns immediately - a broadcast is never synchronous."""
	assert_can_use(api_client, project=project, audience=audience)

	if idempotency_key:
		existing = frappe.db.get_value(
			"Push Campaign", {"idempotency_key": idempotency_key}, "name"
		)
		if existing:
			# A retried request must not produce a second broadcast.
			return {"campaign_id": existing, "duplicate": True}

	if isinstance(data, str):
		data = frappe.parse_json(data)

	campaign = frappe.get_doc(
		{
			"doctype": "Push Campaign",
			"title": title,
			"body": body,
			"fcm_project": project,
			"audience": audience,
			"data_payload": frappe.as_json(data) if data else None,
			"scheduled_at": scheduled_at,
			"idempotency_key": idempotency_key,
			"callback_url": callback_url,
			"api_client": api_client.name,
		}
	)
	campaign.insert(ignore_permissions=True)
	campaign.submit()

	frappe.local.response.http_status_code = 202
	return {"campaign_id": campaign.name, "status": campaign.status}


@frappe.whitelist()
@require_api_client
def campaign_status(campaign_id: str, api_client=None):
	"""Progress for one campaign. Counters only - never tokens or recipients."""
	campaign = frappe.get_doc("Push Campaign", campaign_id)

	if campaign.api_client != api_client.name:
		raise frappe.PermissionError

	return {
		"campaign_id": campaign.name,
		"status": campaign.status,
		"strategy": campaign.strategy,
		"total_recipients": campaign.total_recipients,
		"sent_count": campaign.sent_count,
		"failed_count": campaign.failed_count,
		"invalid_count": campaign.invalid_count,
		"started_at": campaign.started_at,
		"completed_at": campaign.completed_at,
	}
