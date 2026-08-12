# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Remove the User Device doctype, but only when there is nothing to lose.

Push Device replaces it. Installs that still hold rows keep them until the migration
patch ships - deleting somebody's device registrations to tidy up a doctype would be a
poor trade.
"""

import frappe


def execute():
	if not frappe.db.table_exists("User Device"):
		return

	rows = frappe.db.count("User Device")
	if rows:
		frappe.logger("frappe_fcm_notification").warning(
			f"User Device still holds {rows} rows; leaving it in place. "
			"Push Device is the replacement - see docs/MIGRATION.md."
		)
		return

	frappe.delete_doc("DocType", "User Device", force=True, ignore_missing=True)
