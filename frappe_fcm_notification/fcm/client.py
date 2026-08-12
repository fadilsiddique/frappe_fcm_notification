# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Send messages to FCM HTTP v1.

There is no multicast in HTTP v1: registration_ids is gone, the batch endpoint is
retired, and the Admin SDK's sendEach is a loop. Throughput on the token path is
entirely our own concurrency, which is why send_many exists and why topics matter for
real broadcasts.
"""

from concurrent.futures import ThreadPoolExecutor

import frappe
import requests

from frappe_fcm_notification.fcm import FCM_TIMEOUT
from frappe_fcm_notification.fcm.credentials import (
	get_authorized_session,
	get_project_config,
	invalidate,
	resolve_project,
)
from frappe_fcm_notification.fcm.errors import Action, classify

FCM_ENDPOINT = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

# Beyond this, the bottleneck is FCM's own per-connection throughput rather than us.
DEFAULT_MAX_WORKERS = 15


class FCMClient:
	def __init__(self, project: str | None = None):
		self.project = resolve_project(project)
		self.config = get_project_config(self.project)
		self.endpoint = FCM_ENDPOINT.format(project_id=self.config.firebase_project_id)

	@property
	def session(self):
		return get_authorized_session(self.project)

	def send(self, message: dict, _refreshed: bool = False) -> tuple[Action, str, float]:
		"""Send one message. Returns (action, error_code, retry_after_sec)."""
		try:
			response = self.session.post(
				self.endpoint, json={"message": message}, timeout=FCM_TIMEOUT
			)
		except requests.RequestException as e:
			# A timeout or a dropped connection says nothing about the message itself.
			return Action.RETRY, e.__class__.__name__, 0.0

		action, code, retry_after = classify(response.status_code, _safe_json(response), response.headers)

		if action == Action.RETRY and response.status_code in (401, 403) and not _refreshed:
			# Stale credentials in this process. Drop them and try once more before
			# handing a retry back to the caller.
			invalidate(self.project)
			return self.send(message, _refreshed=True)

		return action, code, retry_after

	def send_many(
		self, messages: list[dict], max_workers: int = DEFAULT_MAX_WORKERS
	) -> list[tuple[Action, str, float]]:
		"""Send messages concurrently, preserving input order in the results."""
		if not messages:
			return []

		if len(messages) == 1:
			return [self.send(messages[0])]

		with ThreadPoolExecutor(max_workers=min(max_workers, len(messages))) as pool:
			return list(pool.map(self.send, messages))


def _safe_json(response) -> dict:
	try:
		return response.json() or {}
	except ValueError:
		return {}


def log_failure(context: str, code: str, status_code: int | None = None) -> None:
	"""Record a delivery failure without leaking the recipient.

	FCM echoes the device token back inside INVALID_ARGUMENT responses, and Error Log is
	readable by every System Manager and included in support exports - so error codes go
	in, response bodies never do.
	"""
	frappe.logger("frappe_fcm_notification").warning(
		f"{context}: {code}" + (f" (HTTP {status_code})" if status_code else "")
	)
