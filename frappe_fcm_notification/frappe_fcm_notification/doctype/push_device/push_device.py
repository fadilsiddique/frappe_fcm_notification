# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

import hashlib
import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

# A raw APNS device token: 64 hex characters. Clients used to send these and the server
# converted them through the legacy Instance ID endpoint, which Google retired. Rejecting
# them here is the difference between a clear error at registration and a token that
# silently never delivers.
APNS_TOKEN_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")

PAGE_LENGTH = 1000


class PushDevice(Document):
	def before_insert(self):
		self.set_defaults()

	def validate(self):
		self.token = (self.token or "").strip()
		if not self.token:
			frappe.throw(_("A device token is required."))

		if APNS_TOKEN_PATTERN.match(self.token):
			frappe.throw(
				_(
					"This looks like a raw APNS token. Send the FCM registration token from "
					"messaging().getToken() instead - server-side APNS conversion is no longer "
					"supported."
				)
			)

		self.token_hash = hash_token(self.token)
		# Frappe defaults a Data field to "", and MariaDB has no partial indexes, so every
		# anonymous device would collide on ("", project) in the unique index. NULLs do not
		# collide.
		self.device_id = (self.device_id or "").strip() or None

	def set_defaults(self):
		if not self.fcm_project:
			from frappe_fcm_notification.fcm.credentials import resolve_project

			self.fcm_project = resolve_project()

		if not self.last_seen:
			self.last_seen = now_datetime()


def hash_token(token: str) -> str:
	return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def upsert_device(
	token: str,
	platform: str | None = None,
	user: str | None = None,
	fcm_project: str | None = None,
	device_id: str | None = None,
	device_name: str | None = None,
	app_version: str | None = None,
	locale: str | None = None,
) -> str:
	"""Register a device, updating the existing row when we have seen it before.

	Matched first on the token itself, then on (device_id, project) so that a rotated
	token updates its install rather than accumulating a new row per rotation.
	"""
	from frappe_fcm_notification.fcm.credentials import resolve_project

	token = (token or "").strip()
	fcm_project = resolve_project(fcm_project)
	device_id = (device_id or "").strip() or None

	name = frappe.db.get_value("Push Device", {"token_hash": hash_token(token)}, "name")
	if not name and device_id:
		name = frappe.db.get_value(
			"Push Device", {"device_id": device_id, "fcm_project": fcm_project}, "name"
		)

	values = {
		"token": token,
		"platform": platform,
		"user": user,
		"fcm_project": fcm_project,
		"device_id": device_id,
		"device_name": device_name,
		"app_version": app_version,
		"locale": locale,
	}
	values = {k: v for k, v in values.items() if v is not None}

	if name:
		doc = frappe.get_doc("Push Device", name)
		doc.update(values)
		doc.is_active = 1
		doc.invalidated_on = None
		doc.invalidation_reason = None
		doc.last_seen = now_datetime()
		doc.save(ignore_permissions=True)
		return doc.name

	doc = frappe.get_doc({"doctype": "Push Device", **values})
	doc.insert(ignore_permissions=True)
	return doc.name


def deactivate_device(token: str, reason: str = "UNREGISTERED") -> None:
	deactivate_tokens([token], reason)


def deactivate_tokens(tokens: list[str], reason: str = "UNREGISTERED") -> int:
	"""Retire dead tokens in one statement.

	One save() per device turns a bad-token storm into a write storm; at batch scale this
	has to be a single UPDATE.
	"""
	hashes = [hash_token(t) for t in tokens if t]
	if not hashes:
		return 0

	table = frappe.qb.DocType("Push Device")
	(
		frappe.qb.update(table)
		.set(table.is_active, 0)
		.set(table.invalidated_on, now_datetime())
		.set(table.invalidation_reason, reason[:140])
		.where(table.token_hash.isin(hashes))
		.where(table.is_active == 1)
	).run()

	return len(hashes)


def get_active_tokens(
	user: str | None = None,
	fcm_project: str | None = None,
	limit_start: int = 0,
	page_length: int = PAGE_LENGTH,
) -> list[str]:
	"""Active tokens only. The old code never filtered this and retried dead tokens forever."""
	filters = {"is_active": 1}
	if user:
		filters["user"] = user
	if fcm_project:
		filters["fcm_project"] = fcm_project

	return frappe.get_all(
		"Push Device",
		filters=filters,
		pluck="token",
		order_by="name",
		limit_start=limit_start,
		limit_page_length=page_length,
	)


def iter_active_tokens(fcm_project: str, page_length: int = PAGE_LENGTH):
	"""Page through every active token for a project.

	A generator, not a list: a 200k-row audience materialised in a worker is an OOM.
	"""
	limit_start = 0
	while True:
		tokens = get_active_tokens(
			fcm_project=fcm_project, limit_start=limit_start, page_length=page_length
		)
		if not tokens:
			return
		yield from tokens
		limit_start += page_length
