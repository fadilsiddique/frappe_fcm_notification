# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Composite indexes that a DocType JSON cannot express.

The unique constraint on (device_id, fcm_project) only binds rows that supply a
device_id: MariaDB has no partial indexes, but it does allow repeated NULLs in a unique
index, and PushDevice.validate coerces a blank device_id to NULL for exactly this
reason.
"""

import frappe


def execute():
	if not frappe.db.table_exists("Push Device"):
		return

	# Every send filters on both of these.
	frappe.db.add_index("Push Device", ["fcm_project", "is_active"])

	frappe.db.add_unique("Push Device", ["device_id", "fcm_project"], "unique_device_id_project")
