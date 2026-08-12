# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Message construction rules that FCM enforces server-side, checked before we send."""

import json
import unittest

import frappe

from frappe_fcm_notification.fcm.payload import (
	MAX_MESSAGE_BYTES,
	build_message,
	clean_text,
	coerce_data,
)

PROJECT_CONFIG = frappe._dict(
	firebase_project_id="demo-app",
	default_sound="chime.wav",
	android_channel_id="orders",
	apns_bundle_id="com.example.app",
)


class TestBuildMessage(unittest.TestCase):
	def test_token_message(self):
		message = build_message(
			token="tok-1", title="Hello", body="World", project_config=PROJECT_CONFIG
		)

		self.assertEqual(message["token"], "tok-1")
		self.assertEqual(message["notification"], {"title": "Hello", "body": "World"})
		self.assertNotIn("topic", message)

	def test_project_config_drives_delivery_settings(self):
		"""No client-specific constants: sound and bundle id come from the project."""
		message = build_message(
			token="tok-1", title="t", body="b", project_config=PROJECT_CONFIG
		)

		self.assertEqual(message["android"]["notification"]["sound"], "chime.wav")
		self.assertEqual(message["android"]["notification"]["channel_id"], "orders")
		self.assertEqual(message["apns"]["payload"]["aps"]["sound"], "chime.wav")
		self.assertEqual(message["apns"]["headers"]["apns-topic"], "com.example.app")

	def test_exactly_one_target(self):
		with self.assertRaises(frappe.ValidationError):
			build_message(title="t", body="b", project_config=PROJECT_CONFIG)

		with self.assertRaises(frappe.ValidationError):
			build_message(token="tok", topic="all", title="t", body="b", project_config=PROJECT_CONFIG)

	def test_topic_and_condition_targets(self):
		self.assertEqual(
			build_message(topic="all", title="t", body="b", project_config=PROJECT_CONFIG)["topic"],
			"all",
		)
		self.assertEqual(
			build_message(
				condition="'all' in topics", title="t", body="b", project_config=PROJECT_CONFIG
			)["condition"],
			"'all' in topics",
		)

	def test_html_is_stripped_including_entities(self):
		message = build_message(
			token="tok-1",
			title="<b>Paid</b>",
			body="<p>Total&nbsp;is 10 &amp; rising</p>",
			project_config=PROJECT_CONFIG,
		)

		self.assertEqual(message["notification"]["title"], "Paid")
		self.assertEqual(message["notification"]["body"], "Total is 10 & rising")

	def test_empty_notification_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			build_message(token="tok-1", title="", body="", project_config=PROJECT_CONFIG)

	def test_oversized_payload_names_the_field(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			build_message(
				token="tok-1",
				title="t",
				body="b",
				data={"blob": "x" * (MAX_MESSAGE_BYTES + 100)},
				project_config=PROJECT_CONFIG,
			)

		self.assertIn("data.blob", str(ctx.exception))

	def test_size_check_measures_serialised_bytes(self):
		message = build_message(
			token="tok-1", title="t", body="b", data={"k": "v"}, project_config=PROJECT_CONFIG
		)
		self.assertLessEqual(len(json.dumps(message).encode()), MAX_MESSAGE_BYTES)


class TestCoerceData(unittest.TestCase):
	def test_values_become_strings(self):
		coerced = coerce_data(
			{"count": 3, "flag": True, "nested": {"a": 1}, "items": [1, 2], "skip": None, "s": "x"}
		)

		self.assertEqual(coerced["count"], "3")
		self.assertEqual(coerced["flag"], "true")
		self.assertEqual(json.loads(coerced["nested"]), {"a": 1})
		self.assertEqual(json.loads(coerced["items"]), [1, 2])
		self.assertEqual(coerced["s"], "x")
		self.assertNotIn("skip", coerced)

	def test_all_values_are_strings(self):
		for value in coerce_data({"a": 1, "b": [1], "c": None, "d": False}).values():
			self.assertIsInstance(value, str)


class TestCleanText(unittest.TestCase):
	def test_handles_none_and_markup(self):
		self.assertEqual(clean_text(None), "")
		self.assertEqual(clean_text("  <div>hi</div>  "), "hi")
