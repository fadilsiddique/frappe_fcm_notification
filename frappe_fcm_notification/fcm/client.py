# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Send messages to FCM HTTP v1.

There is no multicast in HTTP v1: registration_ids is gone, the batch endpoint is
retired, and the Admin SDK's sendEach is a loop. Throughput on the token path is
entirely our own concurrency, which is why send_many exists and why topics matter for
real broadcasts.
"""

from concurrent.futures import ThreadPoolExecutor
from functools import partial

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
TOPIC_ENDPOINT = "https://iid.googleapis.com/iid/v1"

# Beyond this, the bottleneck is FCM's own per-connection throughput rather than us.
DEFAULT_MAX_WORKERS = 15

# Batch topic management accepts 1000 tokens per call.
TOPIC_CHUNK = 1000


class FCMClient:
	def __init__(self, project: str | None = None):
		self.project = resolve_project(project)
		self.config = get_project_config(self.project)
		self.endpoint = FCM_ENDPOINT.format(project_id=self.config.firebase_project_id)

	@property
	def session(self):
		return get_authorized_session(self.project)

	def send(self, message: dict) -> tuple[Action, str, float]:
		"""Send one message. Returns (action, error_code, retry_after_sec)."""
		return self.send_many([message])[0]

	def send_many(
		self, messages: list[dict], max_workers: int = DEFAULT_MAX_WORKERS
	) -> list[tuple[Action, str, float]]:
		"""Send messages concurrently, preserving input order in the results."""
		if not messages:
			return []

		responses = self._post_all(messages, max_workers)
		results = [_classify_response(r) for r in responses]

		stale = [
			i
			for i, (response, (action, _code, _retry_after)) in enumerate(zip(responses, results, strict=True))
			if action == Action.RETRY and getattr(response, "status_code", None) in (401, 403)
		]
		if stale:
			# Stale credentials in this process. Drop them and try those once more before
			# handing a retry back to the caller.
			invalidate(self.project)
			retried = self._post_all([messages[i] for i in stale], max_workers)
			for i, response in zip(stale, retried, strict=True):
				results[i] = _classify_response(response)

		return results

	def _post_all(self, messages: list[dict], max_workers: int) -> list:
		"""POST each message, in order. Pool threads only ever do the HTTP call.

		frappe.local is a ContextVar, and pool threads start with an empty context - any
		frappe.cache / frappe.db / frappe.utils call in there raises AttributeError: conf.
		So the session is resolved here, and classification and invalidation happen back
		on the caller's thread.
		"""
		post = partial(_post, self.session, self.endpoint)

		if len(messages) == 1:
			return [post(messages[0])]

		with ThreadPoolExecutor(max_workers=min(max_workers, len(messages))) as pool:
			return list(pool.map(post, messages))


	def subscribe_topic(self, tokens: list[str], topic: str) -> bool:
		return self._topic_call("batchAdd", tokens, topic)

	def unsubscribe_topic(self, tokens: list[str], topic: str) -> bool:
		return self._topic_call("batchRemove", tokens, topic)

	def _topic_call(self, operation: str, tokens: list[str], topic: str) -> bool:
		"""Batch topic management, 1000 tokens per call.

		This is the same host as the retired batchImport endpoint, but a different and
		still-supported operation. Check Google's current guidance before extending it -
		shipping against a dead endpoint is how the old iOS conversion path rotted.
		"""
		tokens = [t for t in tokens if t]
		if not tokens:
			return True

		ok = True
		for start in range(0, len(tokens), TOPIC_CHUNK):
			chunk = tokens[start : start + TOPIC_CHUNK]
			try:
				response = self.session.post(
					f"{TOPIC_ENDPOINT}:{operation}",
					json={"to": f"/topics/{topic}", "registration_tokens": chunk},
					headers={"access_token_auth": "true"},
					timeout=FCM_TIMEOUT,
				)
			except requests.RequestException as e:
				log_failure(f"topic {operation} {topic}", e.__class__.__name__)
				ok = False
				continue

			if response.status_code >= 400:
				log_failure(f"topic {operation} {topic}", "REQUEST_FAILED", response.status_code)
				ok = False

		return ok


def _post(session, endpoint: str, message: dict):
	"""Runs on pool threads: no Frappe calls allowed. Returns the response or the error."""
	try:
		return session.post(endpoint, json={"message": message}, timeout=FCM_TIMEOUT)
	except requests.RequestException as e:
		return e


def _classify_response(response) -> tuple[Action, str, float]:
	if isinstance(response, requests.RequestException):
		# A timeout or a dropped connection says nothing about the message itself.
		return Action.RETRY, response.__class__.__name__, 0.0

	return classify(response.status_code, _safe_json(response), response.headers)


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
