# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Campaign fan-out, retries and token invalidation. FCM is mocked throughout."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_fcm_notification.engine.dispatch import BATCH_SIZE, dispatch_campaign, process_batch
from frappe_fcm_notification.fcm.errors import Action
from frappe_fcm_notification.frappe_fcm_notification.doctype.push_device.push_device import (
	upsert_device,
)

TEST_PROJECT = "_test_dispatch_project"
TEST_AUDIENCE = "_test_dispatch_audience"

SEND_PATH = "frappe_fcm_notification.fcm.client.FCMClient.send"


def ok_send(self, message, _refreshed=False):
	return Action.OK, "", 0.0


def unregistered_send(self, message, _refreshed=False):
	if message["token"] == "disp-dead":
		return Action.DROP_TOKEN, "UNREGISTERED", 0.0
	return Action.OK, "", 0.0


def throttled_send(self, message, _refreshed=False):
	return Action.RETRY, "QUOTA_EXCEEDED", 30.0


class TestDispatch(IntegrationTestCase):
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
					"rate_limit_per_second": 100000,
					"success_sample_rate": 0,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Push Audience", TEST_AUDIENCE):
			frappe.get_doc(
				{
					"doctype": "Push Audience",
					"audience_name": TEST_AUDIENCE,
					"audience_type": "All",
					"strategy_hint": "Force Token",
					"fcm_project": TEST_PROJECT,
				}
			).insert(ignore_permissions=True)

	def make_campaign(self, **kwargs):
		campaign = frappe.get_doc(
			{
				"doctype": "Push Campaign",
				"title": "Test",
				"body": "Body",
				"fcm_project": TEST_PROJECT,
				"audience": TEST_AUDIENCE,
				**kwargs,
			}
		)
		campaign.insert(ignore_permissions=True)
		campaign.db_set("status", "Queued", update_modified=False)
		return campaign

	def make_batch(self, campaign, tokens):
		return frappe.get_doc(
			{
				"doctype": "Push Batch",
				"campaign": campaign.name,
				"batch_index": 0,
				"tokens": json.dumps(tokens),
				"status": "Pending",
			}
		).insert(ignore_permissions=True)

	def test_fan_out_splits_into_batches_of_500(self):
		tokens = [f"disp-bulk-{i}" for i in range(1200)]
		campaign = self.make_campaign()

		with patch(
			"frappe_fcm_notification.engine.dispatch.resolve",
			return_value=(
				__import__(
					"frappe_fcm_notification.engine.audience", fromlist=["Strategy"]
				).Strategy.TOKEN,
				iter(tokens),
			),
		):
			dispatch_campaign(campaign.name)

		batches = frappe.get_all("Push Batch", filters={"campaign": campaign.name}, pluck="name")
		campaign.reload()

		self.assertEqual(len(batches), 3)
		self.assertEqual(campaign.total_recipients, 1200)
		self.assertEqual(BATCH_SIZE, 500)

	def test_unregistered_deactivates_only_that_device(self):
		dead = upsert_device(token="disp-dead", fcm_project=TEST_PROJECT)
		alive = upsert_device(token="disp-alive", fcm_project=TEST_PROJECT)
		campaign = self.make_campaign()
		batch = self.make_batch(campaign, ["disp-dead", "disp-alive"])

		with patch(SEND_PATH, unregistered_send):
			process_batch(batch.name)

		batch.reload()
		self.assertEqual(frappe.db.get_value("Push Device", dead, "is_active"), 0)
		self.assertEqual(frappe.db.get_value("Push Device", alive, "is_active"), 1)
		self.assertEqual(batch.invalid_count, 1)
		self.assertEqual(batch.sent_count, 1)
		self.assertEqual(batch.status, "Done")

	def test_quota_exceeded_requeues_instead_of_failing(self):
		campaign = self.make_campaign()
		batch = self.make_batch(campaign, ["disp-throttled"])

		with patch(SEND_PATH, throttled_send):
			process_batch(batch.name)

		batch.reload()
		self.assertEqual(batch.status, "Pending")
		self.assertEqual(batch.retry_count, 1)
		self.assertTrue(batch.next_retry_at)
		self.assertNotEqual(campaign.reload().status, "Failed")

	def test_retry_only_carries_unsent_tokens(self):
		campaign = self.make_campaign()
		batch = self.make_batch(campaign, ["disp-ok", "disp-retry"])

		def mixed(self, message, _refreshed=False):
			if message["token"] == "disp-retry":
				return Action.RETRY, "UNAVAILABLE", 0.0
			return Action.OK, "", 0.0

		with patch(SEND_PATH, mixed):
			process_batch(batch.name)

		batch.reload()
		self.assertEqual(json.loads(batch.tokens), ["disp-retry"])

	def test_cancelled_campaign_stops_its_batches(self):
		campaign = self.make_campaign()
		batch = self.make_batch(campaign, ["disp-cancelled"])
		campaign.db_set("status", "Cancelled", update_modified=False)

		with patch(SEND_PATH, ok_send):
			process_batch(batch.name)

		batch.reload()
		self.assertEqual(batch.status, "Dead")
		self.assertEqual(batch.sent_count, 0)

	def test_campaign_completes_when_its_last_batch_finishes(self):
		campaign = self.make_campaign()
		campaign.db_set("status", "Running", update_modified=False)
		batch = self.make_batch(campaign, ["disp-final"])

		with patch(SEND_PATH, ok_send):
			process_batch(batch.name)

		campaign.reload()
		self.assertEqual(campaign.status, "Completed")
		self.assertTrue(campaign.completed_at)
		self.assertEqual(campaign.sent_count, 1)

	def test_scheduled_campaign_is_claimed_exactly_once(self):
		"""Two overlapping cron runs must not dispatch the same campaign twice.

		The conditional UPDATE is the lock; the loser matches zero rows. Reading that
		from the cursor matters - the query builder returns an empty result set for an
		UPDATE, which reads as success for everyone.
		"""
		from frappe_fcm_notification.engine.dispatch import _claim_campaign

		campaign = self.make_campaign()

		# _claim_campaign commits so the claim is visible to the competing runner;
		# suppressed here to keep the test inside its transaction.
		with patch.object(frappe.db, "commit"):
			first = _claim_campaign(campaign.name)
			second = _claim_campaign(campaign.name)

		self.assertTrue(first)
		self.assertFalse(second)
		self.assertEqual(campaign.reload().status, "Running")

	def test_failures_are_logged_but_successes_are_sampled(self):
		"""One row per recipient would out-write the sends themselves."""
		campaign = self.make_campaign()
		batch = self.make_batch(campaign, ["disp-dead", "disp-alive"])
		upsert_device(token="disp-dead", fcm_project=TEST_PROJECT)
		upsert_device(token="disp-alive", fcm_project=TEST_PROJECT)

		with patch(SEND_PATH, unregistered_send):
			process_batch(batch.name)

		logs = frappe.get_all(
			"Push Delivery Log", filters={"campaign": campaign.name}, fields=["status"]
		)

		# success_sample_rate is 0 for this project, so only the invalid one is recorded.
		self.assertEqual([log.status for log in logs], ["Invalid"])
