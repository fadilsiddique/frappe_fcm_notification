# Copyright (c) 2024, Upscape Technologies and contributors
# For license information, please see license.txt

import frappe

# Values that were previously hardcoded in the app before they became
# configurable fields on "FCM Notification Settings". Backfill them so
# existing sites keep the exact same behaviour after upgrading.
DEFAULTS = {
	"ios_application_id": "com.upscape.crm",
	"notification_sound": "cash_notification.wav",
}


def execute():
	settings = frappe.get_single("FCM Notification Settings")

	changed = False
	for fieldname, default_value in DEFAULTS.items():
		if not settings.get(fieldname):
			settings.set(fieldname, default_value)
			changed = True

	if changed:
		settings.save(ignore_permissions=True)
