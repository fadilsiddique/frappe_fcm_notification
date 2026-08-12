# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Deprecated entry points, kept for one minor release.

Downstream forks import from this module by name, so the names survive - but nothing
here is whitelisted, and get_fcm_credentials/get_cached_access_token are gone rather
than shimmed: they were the credential disclosure, and a shim invites a fork to
re-expose them.

Everything here routes to frappe_fcm_notification.engine.dispatch. Removed in 2.0.
"""

import frappe

from frappe_fcm_notification.engine.dispatch import on_notification_log, send_transactional
from frappe_fcm_notification.fcm.payload import clean_text
from frappe_fcm_notification.frappe_fcm_notification.doctype.push_device.push_device import (
	get_active_tokens,
)


def _deprecated(old: str, new: str) -> None:
	frappe.logger("frappe_fcm_notification").warning(
		f"{old} is deprecated and will be removed in 2.0. Use {new}."
	)


def notification_queue(doc, method=None):
	"""Deprecated. Routes to engine.dispatch.on_notification_log."""
	_deprecated("fcm_notification.notification_queue", "engine.dispatch.on_notification_log")
	return on_notification_log(doc, method)


def send_fcm_notification(notification, device_token):
	"""Deprecated. Routes to engine.dispatch.send_transactional.

	Accepts what the old callers actually pass: a dict, a frappe._dict or a Document for
	`notification`, and either a token string or a {"device_token": ...} row.
	"""
	_deprecated("fcm_notification.send_fcm_notification", "engine.dispatch.send_transactional")

	if isinstance(device_token, dict):
		device_token = device_token.get("device_token")

	notification = notification or {}
	return send_transactional(
		tokens=[device_token],
		title=notification.get("subject"),
		body=notification.get("email_content"),
		data={
			"doctype": notification.get("document_type") or "",
			"docname": str(notification.get("document_name") or ""),
		},
	)


def cleanhtml(raw_html):
	"""Deprecated. Use fcm.payload.clean_text, which also resolves HTML entities."""
	_deprecated("fcm_notification.cleanhtml", "fcm.payload.clean_text")
	return clean_text(raw_html)


def user_id(doc):
	"""Deprecated. Returns active device tokens for the notification's user."""
	_deprecated("fcm_notification.user_id", "push_device.get_active_tokens")
	return [{"device_token": token} for token in get_active_tokens(user=doc.for_user)]
