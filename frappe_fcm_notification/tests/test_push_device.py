# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Device registration lifecycle: upsert, rotation, invalidation, active-only reads."""

import frappe
from frappe.tests import IntegrationTestCase

from frappe_fcm_notification.frappe_fcm_notification.doctype.push_device.push_device import (
	deactivate_tokens,
	get_active_tokens,
	hash_token,
	upsert_device,
)

TEST_PROJECT = "_test_fcm_project"


class TestPushDevice(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("FCM Project", TEST_PROJECT):
			frappe.get_doc(
				{
					"doctype": "FCM Project",
					"project_name": TEST_PROJECT,
					"firebase_project_id": "demo-app",
					# Site Config source keeps credentials out of the test entirely.
					"credentials_source": "Site Config",
					"default_sound": "chime.wav",
				}
			).insert(ignore_permissions=True)

	def register(self, token, **kwargs):
		kwargs.setdefault("fcm_project", TEST_PROJECT)
		kwargs.setdefault("platform", "android")
		return upsert_device(token=token, **kwargs)

	def test_registration_sets_token_hash_and_is_active(self):
		name = self.register("token-alpha", user="Administrator")
		doc = frappe.get_doc("Push Device", name)

		self.assertEqual(doc.token_hash, hash_token("token-alpha"))
		self.assertEqual(doc.is_active, 1)
		self.assertTrue(doc.last_seen)

	def test_same_token_updates_instead_of_duplicating(self):
		first = self.register("token-beta", user="Administrator", app_version="1.0.0")
		second = self.register("token-beta", user="Administrator", app_version="1.1.0")

		self.assertEqual(first, second)
		self.assertEqual(frappe.db.get_value("Push Device", first, "app_version"), "1.1.0")

	def test_token_rotation_updates_the_install(self):
		"""A new token for a known device_id must not create a second row."""
		first = self.register("token-old", device_id="install-1")
		second = self.register("token-new", device_id="install-1")

		self.assertEqual(first, second)
		self.assertEqual(frappe.db.get_value("Push Device", first, "token"), "token-new")

	def test_blank_device_id_stored_as_null(self):
		"""Empty strings would collide in the (device_id, fcm_project) unique index."""
		one = self.register("token-anon-1")
		two = self.register("token-anon-2")

		self.assertNotEqual(one, two)
		self.assertIsNone(frappe.db.get_value("Push Device", one, "device_id"))
		self.assertIsNone(frappe.db.get_value("Push Device", two, "device_id"))

	def test_apns_token_is_rejected_with_guidance(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.register("a" * 64, platform="ios")

		self.assertIn("messaging().getToken()", str(ctx.exception))

	def test_deactivate_retires_only_the_named_tokens(self):
		dead = self.register("token-dead", user="Administrator")
		alive = self.register("token-alive", user="Administrator")

		deactivate_tokens(["token-dead"], reason="UNREGISTERED")

		self.assertEqual(frappe.db.get_value("Push Device", dead, "is_active"), 0)
		self.assertEqual(
			frappe.db.get_value("Push Device", dead, "invalidation_reason"), "UNREGISTERED"
		)
		self.assertTrue(frappe.db.get_value("Push Device", dead, "invalidated_on"))
		self.assertEqual(frappe.db.get_value("Push Device", alive, "is_active"), 1)

	def test_reads_exclude_inactive_devices(self):
		self.register("token-live", user="Administrator")
		self.register("token-retired", user="Administrator")
		deactivate_tokens(["token-retired"])

		tokens = get_active_tokens(user="Administrator", fcm_project=TEST_PROJECT)

		self.assertIn("token-live", tokens)
		self.assertNotIn("token-retired", tokens)

	def test_reregistering_a_dead_token_revives_it(self):
		name = self.register("token-revive", user="Administrator")
		deactivate_tokens(["token-revive"])

		self.register("token-revive", user="Administrator")

		doc = frappe.get_doc("Push Device", name)
		self.assertEqual(doc.is_active, 1)
		self.assertIsNone(doc.invalidated_on)
