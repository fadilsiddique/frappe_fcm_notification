# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Rebuild the credentials that used to live in nine fields on a Single.

Only does anything on installs that were configured before 1.0. A site that never set a
private key - or that already has an FCM Project - is left alone.
"""

import json

import frappe

FIELDS = (
	"project_id",
	"private_key_id",
	"client_email",
	"client_id",
	"auth_uri",
	"token_uri",
	"auth_provider_x509_cert_url",
	"client_x509_cert_url",
	"universe_domain",
)


def execute():
	if frappe.db.exists("FCM Project", {"name": ("is", "set")}):
		return

	settings = frappe.get_single("FCM Notification Settings")
	private_key = settings.get_password("private_key", raise_exception=False)

	if not private_key or not settings.get("project_id"):
		return

	info = {"type": "service_account", "private_key": private_key.replace("\\n", "\n").strip()}
	for field in FIELDS:
		value = settings.get(field)
		if value:
			info[field] = value

	frappe.get_doc(
		{
			"doctype": "FCM Project",
			"project_name": "default",
			"firebase_project_id": settings.get("project_id"),
			"credentials_source": "DocType",
			"service_account_json": json.dumps(info),
			# The values that used to be hardcoded, now per project.
			"default_sound": settings.get("notification_sound"),
			"apns_bundle_id": settings.get("ios_application_id"),
			"is_default": 1,
			"is_active": 1,
		}
	).insert(ignore_permissions=True)

	frappe.logger("frappe_fcm_notification").info(
		"migrated FCM Notification Settings into FCM Project 'default'"
	)
