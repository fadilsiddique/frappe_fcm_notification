# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""A fake FCM at the HTTP boundary, so FCMClient's own concurrency runs for real in tests."""

from contextlib import contextmanager
from unittest.mock import patch

import frappe

SESSION_PATH = "frappe_fcm_notification.fcm.client.get_authorized_session"


class FakeResponse:
	def __init__(self, status_code: int, body: dict | None = None, headers: dict | None = None):
		self.status_code = status_code
		self._body = body or {}
		self.headers = headers or {}

	def json(self):
		return self._body


class FakeSession:
	def __init__(self, respond):
		self.respond = respond

	def post(self, endpoint, json=None, timeout=None):
		return self.respond(json["message"])


@contextmanager
def fake_fcm(respond):
	"""Route every send through `respond(message) -> FakeResponse`.

	Session lookup touches frappe.cache, as the real one does, so anything that resolves
	the session off the site context (e.g. on a pool thread) fails here as it would live.
	"""

	def get_session(project=None):
		frappe.cache.get_value(f"fcm:cred_version:{project}")
		return FakeSession(respond)

	with patch(SESSION_PATH, get_session):
		yield


def error(status_code: int, status: str, error_code: str | None = None, headers: dict | None = None):
	details = [{"errorCode": error_code}] if error_code else []
	return FakeResponse(status_code, {"error": {"status": status, "details": details}}, headers)


def ok(message=None):
	return FakeResponse(200, {"name": "projects/demo-app/messages/1"})
