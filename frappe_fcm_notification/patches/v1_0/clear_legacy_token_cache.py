# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Blank the OAuth token that used to be cached on the Single.

The cache lives in Redis now. A stale bearer token sitting in the database is a
credential nobody is watching.
"""

import frappe


def execute():
	if not frappe.db.exists("DocType", "FCM Notification Settings"):
		return

	settings = frappe.get_single("FCM Notification Settings")
	if not (settings.get("access_token") or settings.get("expiration_time")):
		return

	settings.db_set("access_token", None, update_modified=False)
	settings.db_set("expiration_time", None, update_modified=False)

	frappe.db.delete(
		"__Auth",
		{"doctype": "FCM Notification Settings", "fieldname": "access_token"},
	)
