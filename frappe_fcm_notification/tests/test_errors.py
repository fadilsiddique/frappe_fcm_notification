# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Table-driven coverage of the FCM response classifier."""

import unittest

from frappe_fcm_notification.fcm.errors import Action, backoff_seconds, classify, retry_after_seconds


def envelope(status: str, error_code: str | None = None, field: str | None = None) -> dict:
	details = []
	if error_code:
		details.append({"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": error_code})
	if field:
		details.append({"fieldViolations": [{"field": field, "description": "bad"}]})
	return {"error": {"status": status, "message": "boom", "details": details}}


class TestClassify(unittest.TestCase):
	def test_mapping(self):
		cases = [
			# (label, status_code, body, expected action, expected code)
			("success", 200, {}, Action.OK, ""),
			(
				"unregistered token",
				404,
				envelope("NOT_FOUND", "UNREGISTERED"),
				Action.DROP_TOKEN,
				"UNREGISTERED",
			),
			(
				"token from another firebase project",
				403,
				envelope("PERMISSION_DENIED", "SENDER_ID_MISMATCH"),
				Action.DROP_TOKEN,
				"SENDER_ID_MISMATCH",
			),
			(
				"malformed token",
				400,
				envelope("INVALID_ARGUMENT", "INVALID_ARGUMENT", field="message.token"),
				Action.DROP_TOKEN,
				"INVALID_ARGUMENT",
			),
			(
				"malformed payload is never retried",
				400,
				envelope("INVALID_ARGUMENT", "INVALID_ARGUMENT", field="message.android.ttl"),
				Action.FAIL,
				"INVALID_ARGUMENT",
			),
			("stale credentials", 401, envelope("UNAUTHENTICATED"), Action.RETRY, "UNAUTHENTICATED"),
			(
				"quota exceeded",
				429,
				envelope("RESOURCE_EXHAUSTED", "QUOTA_EXCEEDED"),
				Action.RETRY,
				"QUOTA_EXCEEDED",
			),
			(
				"backend unavailable",
				503,
				envelope("UNAVAILABLE", "UNAVAILABLE"),
				Action.RETRY,
				"UNAVAILABLE",
			),
			("internal error", 500, envelope("INTERNAL", "INTERNAL"), Action.RETRY, "INTERNAL"),
		]

		for label, status_code, body, expected_action, expected_code in cases:
			with self.subTest(label):
				action, code, _retry_after = classify(status_code, body)
				self.assertEqual(action, expected_action)
				self.assertEqual(code, expected_code)

	def test_empty_body_falls_back_to_status(self):
		action, code, _ = classify(503, None)
		self.assertEqual(action, Action.RETRY)
		self.assertEqual(code, "HTTP_503")

	def test_403_without_error_code_retries_once(self):
		# A plain 403 is ambiguous - stale token or genuine denial - so it is retried
		# after a refresh rather than dropping a possibly-good device.
		action, _code, _ = classify(403, {})
		self.assertEqual(action, Action.RETRY)

	def test_retry_after_seconds(self):
		self.assertEqual(retry_after_seconds({"Retry-After": "30"}), 30.0)
		self.assertEqual(retry_after_seconds({}), 0.0)
		self.assertEqual(retry_after_seconds(None), 0.0)
		self.assertEqual(retry_after_seconds({"Retry-After": "nonsense"}), 0.0)

	def test_429_honours_retry_after(self):
		_action, _code, retry_after = classify(
			429, envelope("RESOURCE_EXHAUSTED", "QUOTA_EXCEEDED"), {"Retry-After": "12"}
		)
		self.assertEqual(retry_after, 12.0)

	def test_backoff_grows_and_respects_retry_after(self):
		self.assertLessEqual(backoff_seconds(1), 2.0)
		self.assertGreaterEqual(backoff_seconds(5, retry_after=90), 90)
		self.assertGreater(backoff_seconds(6), backoff_seconds(0))
