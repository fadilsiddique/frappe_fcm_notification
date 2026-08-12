# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Classify FCM HTTP v1 responses into an action.

FCM reports failures through an error envelope, not the HTTP status alone:

    {"error": {"code": 404, "status": "NOT_FOUND", "message": "...",
               "details": [{"@type": ".../FcmError", "errorCode": "UNREGISTERED"}]}}

The errorCode in `details` is the authoritative value - the same HTTP 400 can mean
"this token is garbage, drop it" or "your payload is malformed, never retry it".
"""

import random
from email.utils import parsedate_to_datetime
from enum import StrEnum

from frappe.utils import now_datetime


class Action(StrEnum):
	OK = "OK"
	RETRY = "RETRY"
	DROP_TOKEN = "DROP_TOKEN"
	FAIL = "FAIL"


# errorCode values that mean the token will never be deliverable again.
DEAD_TOKEN_CODES = {
	"UNREGISTERED",
	"SENDER_ID_MISMATCH",
	"INVALID_REGISTRATION",
	"NOT_FOUND",
}

# errorCode values that are worth another attempt.
TRANSIENT_CODES = {
	"UNAVAILABLE",
	"INTERNAL",
	"QUOTA_EXCEEDED",
	"SERVER_UNAVAILABLE",
	"MESSAGE_RATE_EXCEEDED",
	"DEVICE_MESSAGE_RATE_EXCEEDED",
	"TOPICS_MESSAGE_RATE_EXCEEDED",
	"THIRD_PARTY_AUTH_ERROR",
}

MAX_BACKOFF_SEC = 300.0


def classify(status_code: int, body: dict | None, headers=None) -> tuple[Action, str, float]:
	"""Return (action, error_code, retry_after_sec) for one FCM response.

	`retry_after_sec` is 0 for anything that is not a RETRY.
	"""
	if status_code == 200:
		return Action.OK, "", 0.0

	error = ((body or {}).get("error")) or {}
	code = _error_code(error) or error.get("status") or f"HTTP_{status_code}"

	if code in DEAD_TOKEN_CODES:
		return Action.DROP_TOKEN, code, 0.0

	if status_code == 400:
		# A violation on the token field means this recipient is bad; anything else is a
		# payload bug that will fail identically forever.
		if _violates_token_field(error):
			return Action.DROP_TOKEN, code, 0.0
		return Action.FAIL, code, 0.0

	if status_code in (401, 403):
		# Stale or rejected credentials. The caller refreshes and retries once; a genuine
		# permission problem then classifies the same way and stops at max_retries.
		return Action.RETRY, code, 0.0

	if status_code == 404:
		return Action.DROP_TOKEN, code, 0.0

	if status_code == 429 or code in TRANSIENT_CODES or status_code >= 500:
		return Action.RETRY, code, retry_after_seconds(headers)

	return Action.FAIL, code, 0.0


def _error_code(error: dict) -> str:
	for detail in error.get("details") or []:
		if isinstance(detail, dict) and detail.get("errorCode"):
			return detail["errorCode"]
	return ""


def _violates_token_field(error: dict) -> bool:
	for detail in error.get("details") or []:
		if not isinstance(detail, dict):
			continue
		for violation in detail.get("fieldViolations") or []:
			if str(violation.get("field", "")).startswith("message.token"):
				return True
	return False


def retry_after_seconds(headers=None) -> float:
	"""Honour Retry-After, which FCM sends as either seconds or an HTTP date."""
	value = (headers or {}).get("Retry-After") if headers else None
	if not value:
		return 0.0

	try:
		return max(0.0, float(value))
	except (TypeError, ValueError):
		pass

	try:
		delta = (parsedate_to_datetime(value).replace(tzinfo=None) - now_datetime()).total_seconds()
		return max(0.0, min(delta, MAX_BACKOFF_SEC))
	except (TypeError, ValueError):
		return 0.0


def backoff_seconds(attempt: int, retry_after: float = 0.0) -> float:
	"""Exponential backoff with jitter, never below an explicit Retry-After."""
	base = min(2.0**attempt, MAX_BACKOFF_SEC)
	return max(retry_after, base * (0.5 + random.random() / 2))
