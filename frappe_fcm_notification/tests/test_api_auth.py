# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Scoping for external API clients, and the SSRF guard on callback URLs."""

import hmac
from hashlib import sha256

import frappe
from frappe.tests import IntegrationTestCase

from frappe_fcm_notification.api.auth import assert_can_use
from frappe_fcm_notification.engine.webhooks import assert_safe_callback_url, sign

PROJECT_A = "_test_auth_project_a"
PROJECT_B = "_test_auth_project_b"


class TestApiClientScoping(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for project in (PROJECT_A, PROJECT_B):
			if not frappe.db.exists("FCM Project", project):
				frappe.get_doc(
					{
						"doctype": "FCM Project",
						"project_name": project,
						"firebase_project_id": "demo-app",
						"credentials_source": "Site Config",
					}
				).insert(ignore_permissions=True)

		if not frappe.db.exists("Push Audience", "_test_auth_audience"):
			frappe.get_doc(
				{
					"doctype": "Push Audience",
					"audience_name": "_test_auth_audience",
					"audience_type": "All",
					"fcm_project": PROJECT_A,
				}
			).insert(ignore_permissions=True)

	def client(self, **kwargs):
		name = kwargs.pop("client_name")
		if frappe.db.exists("Push API Client", name):
			frappe.delete_doc("Push API Client", name, force=True)

		return frappe.get_doc(
			{
				"doctype": "Push API Client",
				"client_name": name,
				"fcm_project": PROJECT_A,
				**kwargs,
			}
		).insert(ignore_permissions=True)

	def test_credentials_are_generated(self):
		client = self.client(client_name="_test_client_keys")

		self.assertTrue(client.api_key)
		self.assertTrue(client.get_password("api_secret"))

	def test_client_cannot_send_through_another_project(self):
		client = self.client(client_name="_test_client_project")

		with self.assertRaises(frappe.PermissionError):
			assert_can_use(client, project=PROJECT_B)

		assert_can_use(client, project=PROJECT_A)

	def test_no_granted_audiences_means_none_not_all(self):
		client = self.client(client_name="_test_client_no_audience")

		with self.assertRaises(frappe.PermissionError):
			assert_can_use(client, project=PROJECT_A, audience="_test_auth_audience")

	def test_granted_audience_is_permitted(self):
		client = self.client(
			client_name="_test_client_with_audience",
			allowed_audiences=[{"audience": "_test_auth_audience"}],
		)

		assert_can_use(client, project=PROJECT_A, audience="_test_auth_audience")

	def test_audience_from_another_project_is_refused_at_save(self):
		frappe.get_doc(
			{
				"doctype": "Push Audience",
				"audience_name": "_test_auth_audience_b",
				"audience_type": "All",
				"fcm_project": PROJECT_B,
			}
		).insert(ignore_permissions=True)

		with self.assertRaises(frappe.ValidationError):
			self.client(
				client_name="_test_client_cross_project",
				allowed_audiences=[{"audience": "_test_auth_audience_b"}],
			)


class TestSignature(IntegrationTestCase):
	def test_signature_matches_the_documented_scheme(self):
		secret = "s3cret"
		body = '{"a":1}'
		timestamp = "1750000000"

		expected = hmac.new(
			secret.encode(), f"{timestamp}.{body}".encode(), sha256
		).hexdigest()

		self.assertEqual(sign(secret, timestamp, body), expected)

	def test_signature_changes_with_body(self):
		self.assertNotEqual(sign("k", "1", '{"a":1}'), sign("k", "1", '{"a":2}'))

	def test_signature_changes_with_timestamp(self):
		"""Otherwise a captured request could be replayed with a fresh timestamp."""
		self.assertNotEqual(sign("k", "1", "{}"), sign("k", "2", "{}"))


class TestCallbackUrlGuard(IntegrationTestCase):
	def test_rejects_non_http_schemes(self):
		with self.assertRaises(frappe.ValidationError):
			assert_safe_callback_url("file:///etc/passwd")

		with self.assertRaises(frappe.ValidationError):
			assert_safe_callback_url("gopher://example.com/")

	def test_accepts_public_https(self):
		assert_safe_callback_url("https://example.com/hooks/push")

	def test_rejects_missing_host(self):
		with self.assertRaises(frappe.ValidationError):
			assert_safe_callback_url("https:///nohost")
