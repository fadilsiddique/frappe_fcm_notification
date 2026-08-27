# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Credential handling: the key is encrypted, and the input fields never persist it."""

import json

import frappe
from frappe.tests import IntegrationTestCase

SERVICE_ACCOUNT = {
	"type": "service_account",
	"project_id": "demo-app",
	"private_key_id": "abcdef1234567890",
	"private_key": "-----BEGIN PRIVATE KEY-----\\nMIIfake\\n-----END PRIVATE KEY-----\\n",
	"client_email": "pusher@demo-app.iam.gserviceaccount.com",
	"client_id": "1234567890",
	"token_uri": "https://oauth2.googleapis.com/token",
}


class TestFCMProjectCredentials(IntegrationTestCase):
	def project(self, name, **kwargs):
		if frappe.db.exists("FCM Project", name):
			frappe.delete_doc("FCM Project", name, force=True)

		return frappe.get_doc(
			{
				"doctype": "FCM Project",
				"project_name": name,
				"credentials_source": "DocType",
				**kwargs,
			}
		).insert(ignore_permissions=True)

	def test_pasted_json_is_stored_and_readable(self):
		doc = self.project("_test_paste", service_account_paste=json.dumps(SERVICE_ACCOUNT))

		stored = json.loads(doc.get_password("service_account_json"))

		self.assertEqual(stored["client_email"], SERVICE_ACCOUNT["client_email"])
		# The escaped newlines from the JSON file become real ones, or google-auth
		# rejects the key.
		self.assertIn("\n", stored["private_key"])

	def test_paste_field_is_never_written_to_the_table(self):
		doc = self.project("_test_paste_cleared", service_account_paste=json.dumps(SERVICE_ACCOUNT))

		row = frappe.db.sql(
			"select service_account_paste, service_account_json from `tabFCM Project` where name = %s",
			doc.name,
			as_dict=True,
		)[0]

		self.assertFalse(row.service_account_paste)
		# The column holds Frappe's mask; the value itself lives encrypted in __Auth.
		self.assertTrue(set(row.service_account_json or "") <= {"*"})

	def test_private_key_is_not_stored_in_the_doctype_table(self):
		"""The whole point: no part of the key is readable from this table."""
		doc = self.project("_test_no_plaintext", service_account_paste=json.dumps(SERVICE_ACCOUNT))

		row = frappe.db.sql(
			"select * from `tabFCM Project` where name = %s", doc.name, as_dict=True
		)[0]

		for value in row.values():
			self.assertNotIn("BEGIN PRIVATE KEY", str(value))

	def test_firebase_project_id_is_taken_from_the_key(self):
		doc = self.project("_test_project_id", service_account_paste=json.dumps(SERVICE_ACCOUNT))

		self.assertEqual(doc.firebase_project_id, "demo-app")

	def test_mismatched_project_id_is_refused(self):
		"""A key from another project is the usual cause of SENDER_ID_MISMATCH."""
		with self.assertRaises(frappe.ValidationError):
			self.project(
				"_test_mismatch",
				firebase_project_id="some-other-app",
				service_account_paste=json.dumps(SERVICE_ACCOUNT),
			)

	def test_malformed_json_is_refused(self):
		with self.assertRaises(Exception):
			self.project("_test_bad_json", service_account_paste="{not json")

	def test_non_service_account_key_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.project(
				"_test_wrong_type",
				service_account_paste=json.dumps({**SERVICE_ACCOUNT, "type": "authorized_user"}),
			)

	def test_resaving_does_not_clobber_the_stored_key(self):
		"""A loaded document carries '****'; saving it must not store the mask."""
		doc = self.project("_test_resave", service_account_paste=json.dumps(SERVICE_ACCOUNT))

		reloaded = frappe.get_doc("FCM Project", doc.name)
		reloaded.default_sound = "chime.wav"
		reloaded.save(ignore_permissions=True)

		stored = json.loads(frappe.get_doc("FCM Project", doc.name).get_password("service_account_json"))
		self.assertEqual(stored["client_email"], SERVICE_ACCOUNT["client_email"])

	def test_summary_identifies_the_key_without_revealing_it(self):
		doc = self.project("_test_summary", service_account_paste=json.dumps(SERVICE_ACCOUNT))

		self.assertIn("pusher@demo-app.iam.gserviceaccount.com", doc.credentials_summary)
		self.assertNotIn("PRIVATE KEY", doc.credentials_summary)

	def test_site_config_source_needs_no_stored_key(self):
		# The project id is still declared here; only the key itself lives outside the DB.
		doc = self.project(
			"_test_site_config", credentials_source="Site Config", firebase_project_id="demo-app"
		)

		self.assertFalse(doc.get_password("service_account_json", raise_exception=False))
