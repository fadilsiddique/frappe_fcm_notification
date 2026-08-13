# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Audience resolution: strategy choice, streaming, and resolver allowlisting."""

import types

import frappe
from frappe.tests import IntegrationTestCase

from frappe_fcm_notification.engine.audience import Strategy, iter_tokens, resolve
from frappe_fcm_notification.frappe_fcm_notification.doctype.push_device.push_device import (
	upsert_device,
)

TEST_PROJECT = "_test_audience_project"


def audience(**kwargs):
	return frappe._dict({"audience_type": "All", "strategy_hint": "Auto", "users": [], **kwargs})


class TestAudience(IntegrationTestCase):
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

	def test_all_audience_prefers_a_topic(self):
		strategy, target = resolve(audience(audience_type="All"), TEST_PROJECT)

		self.assertEqual(strategy, Strategy.TOPIC)
		self.assertEqual(target, "all")

	def test_topic_audience_uses_its_topic(self):
		strategy, target = resolve(audience(audience_type="Topic", topic="promos"), TEST_PROJECT)

		self.assertEqual(strategy, Strategy.TOPIC)
		self.assertEqual(target, "promos")

	def test_force_token_overrides_the_topic_path(self):
		strategy, target = resolve(
			audience(audience_type="All", strategy_hint="Force Token"), TEST_PROJECT
		)

		self.assertEqual(strategy, Strategy.TOKEN)
		self.assertIsInstance(target, types.GeneratorType)

	def test_role_audience_uses_tokens(self):
		strategy, _target = resolve(
			audience(audience_type="Role", role="System Manager"), TEST_PROJECT
		)

		self.assertEqual(strategy, Strategy.TOKEN)

	def test_token_side_is_always_a_generator(self):
		"""A materialised audience is an OOM at scale, so this must never be a list."""
		result = iter_tokens(audience(audience_type="All"), TEST_PROJECT)

		self.assertIsInstance(result, types.GeneratorType)

	def test_generator_is_lazy(self):
		"""Nothing is queried until the first item is pulled."""
		upsert_device(token="aud-token-1", fcm_project=TEST_PROJECT)
		stream = iter_tokens(audience(audience_type="All"), TEST_PROJECT)

		first = next(stream)

		self.assertTrue(first)

	def test_user_list_audience_resolves_its_users(self):
		upsert_device(token="aud-token-admin", user="Administrator", fcm_project=TEST_PROJECT)

		tokens = list(
			iter_tokens(
				audience(audience_type="User List", users=[frappe._dict(user="Administrator")]),
				TEST_PROJECT,
			)
		)

		self.assertIn("aud-token-admin", tokens)

	def test_unlisted_resolver_method_is_refused(self):
		"""Arbitrary dotted paths from a document would be remote code execution."""
		with self.assertRaises(frappe.ValidationError):
			list(
				iter_tokens(
					audience(audience_type="Method", resolver_method="os.system"), TEST_PROJECT
				)
			)

	def test_audience_doctype_rejects_unlisted_resolver_on_save(self):
		doc = frappe.get_doc(
			{
				"doctype": "Push Audience",
				"audience_name": "_test_bad_resolver",
				"audience_type": "Method",
				"fcm_project": TEST_PROJECT,
				"resolver_method": "subprocess.check_output",
			}
		)

		with self.assertRaises(frappe.ValidationError):
			doc.insert(ignore_permissions=True)

	def test_topic_charset_is_enforced(self):
		doc = frappe.get_doc(
			{
				"doctype": "Push Audience",
				"audience_name": "_test_bad_topic",
				"audience_type": "Topic",
				"fcm_project": TEST_PROJECT,
				"topic": "not a valid topic!",
			}
		)

		with self.assertRaises(frappe.ValidationError):
			doc.insert(ignore_permissions=True)
