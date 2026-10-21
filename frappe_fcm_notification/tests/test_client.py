# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""FCMClient sending, with FCM faked at the HTTP boundary."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_fcm_notification.fcm.client import FCMClient
from frappe_fcm_notification.fcm.errors import Action
from frappe_fcm_notification.tests.fake_fcm import error, fake_fcm, ok

TEST_PROJECT = "_test_client_project"


class TestFCMClient(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()

		if not frappe.db.exists("FCM Project", TEST_PROJECT):
			frappe.get_doc(
				{
					"doctype": "FCM Project",
					"project_name": TEST_PROJECT,
					"firebase_project_id": "demo-app",
					"credentials_source": "Site Config",
				}
			).insert(ignore_permissions=True)

	def test_send_many_to_several_devices(self):
		"""A customer with an iPhone and an Android phone has two tokens.

		Regression: the pool threads resolved the session themselves, which reads
		frappe.cache with no site context there and raised AttributeError: conf.
		"""
		messages = [{"token": "ios"}, {"token": "android"}, {"token": "dead"}]

		def respond(message):
			if message["token"] == "dead":
				return error(404, "NOT_FOUND", "UNREGISTERED")
			return ok()

		with fake_fcm(respond):
			results = FCMClient(TEST_PROJECT).send_many(messages)

		self.assertEqual(
			[action for action, _code, _retry_after in results],
			[Action.OK, Action.OK, Action.DROP_TOKEN],
		)

	def test_http_date_retry_after_on_several_devices(self):
		"""Classifying an HTTP-date Retry-After needs the site timezone - not on a pool thread."""
		headers = {"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}

		with fake_fcm(lambda message: error(503, "UNAVAILABLE", "UNAVAILABLE", headers)):
			results = FCMClient(TEST_PROJECT).send_many([{"token": "a"}, {"token": "b"}])

		self.assertEqual([action for action, _code, _retry_after in results], [Action.RETRY, Action.RETRY])
		self.assertTrue(all(retry_after > 0 for _action, _code, retry_after in results))

	def test_stale_credentials_are_refreshed_once(self):
		seen = []

		def respond(message):
			seen.append(message["token"])
			# Reject each token's first attempt only.
			if seen.count(message["token"]) == 1:
				return error(401, "UNAUTHENTICATED")
			return ok()

		with (
			fake_fcm(respond),
			patch("frappe_fcm_notification.fcm.client.invalidate") as invalidate,
		):
			results = FCMClient(TEST_PROJECT).send_many([{"token": "a"}, {"token": "b"}])

		invalidate.assert_called_once_with(TEST_PROJECT)
		self.assertEqual([action for action, _code, _retry_after in results], [Action.OK, Action.OK])
		self.assertEqual(sorted(seen), ["a", "a", "b", "b"])

	def test_rejected_credentials_stop_after_one_refresh(self):
		with (
			fake_fcm(lambda message: error(403, "PERMISSION_DENIED")),
			patch("frappe_fcm_notification.fcm.client.invalidate") as invalidate,
		):
			action, _code, _retry_after = FCMClient(TEST_PROJECT).send({"token": "a"})

		invalidate.assert_called_once()
		self.assertEqual(action, Action.RETRY)
