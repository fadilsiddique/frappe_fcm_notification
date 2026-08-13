# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Copy User Device rows into Push Device.

is_active is forced to 1 regardless of the old value. The old field was never written
by anything, so its 0 default carries no information - honouring it would deactivate
every device on the site. Dead tokens are retired by the first send instead, which
classifies FCM's response.
"""

import re

import frappe
from frappe.utils import now_datetime

from frappe_fcm_notification.frappe_fcm_notification.doctype.push_device.push_device import (
	hash_token,
)

APNS_TOKEN_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
CHUNK = 5000


def execute():
	if not frappe.db.table_exists("User Device"):
		return

	project = frappe.db.get_value("FCM Project", {"is_default": 1}, "name")
	if not project:
		frappe.logger("frappe_fcm_notification").warning(
			"no default FCM Project; skipping User Device migration"
		)
		return

	rows = frappe.db.sql(
		"""
		select name, user, device_name, device_token, device_type, modified
		from `tabUser Device`
		where device_token is not null and device_token != ''
		order by modified desc
		""",
		as_dict=True,
	)

	seen: set[str] = set(
		frappe.get_all("Push Device", pluck="token_hash", limit_page_length=0) or []
	)
	collisions = skipped_apns = migrated = 0

	for index, row in enumerate(rows, start=1):
		token = (row.device_token or "").strip()
		if not token:
			continue

		if APNS_TOKEN_PATTERN.match(token):
			# Raw APNS tokens were never deliverable through HTTP v1; importing them
			# would only produce guaranteed failures.
			skipped_apns += 1
			continue

		token_hash = hash_token(token)
		if token_hash in seen:
			# Ordered newest first, so the row we already kept is the current one.
			collisions += 1
			continue

		seen.add(token_hash)
		frappe.get_doc(
			{
				"doctype": "Push Device",
				"token": token,
				"user": row.user,
				"device_name": row.device_name,
				"platform": row.device_type,
				"fcm_project": project,
				"is_active": 1,
				"last_seen": row.modified or now_datetime(),
			}
		).insert(ignore_permissions=True)
		migrated += 1

		if index % CHUNK == 0:
			frappe.db.commit()

	frappe.db.commit()
	frappe.logger("frappe_fcm_notification").info(
		f"User Device migration: {migrated} migrated, {collisions} duplicate tokens dropped, "
		f"{skipped_apns} raw APNS tokens skipped"
	)
