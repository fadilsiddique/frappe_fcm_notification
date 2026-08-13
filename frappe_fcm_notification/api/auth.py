# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Scoping for external callers.

The caller authenticates to Frappe normally - `Authorization: token key:secret` on a
user holding the Push API Client role - because a whitelisted endpoint rejects Guests
before any decorator here would run. This layer adds what a Frappe API key cannot
express: which Firebase project, which audiences, from which addresses, how often.

Rationale: a leaked credential belonging to one integration must not be able to
broadcast to another client's user base.
"""

import hmac
import ipaddress
from functools import wraps
from hashlib import sha256

import frappe
from frappe import _
from frappe.utils import cint

# Signatures older than this are refused, so a captured request cannot be replayed.
MAX_CLOCK_SKEW_SEC = 300


def require_api_client(fn):
	"""Resolve and verify the calling Push API Client, then run the endpoint.

	The client is passed to the endpoint as `api_client`.
	"""

	@wraps(fn)
	def wrapper(*args, **kwargs):
		client = authenticate()
		kwargs["api_client"] = client
		return fn(*args, **kwargs)

	return wrapper


def authenticate():
	key = _header("X-Push-Key")
	signature = _header("X-Push-Signature")
	timestamp = _header("X-Push-Timestamp")

	if not (key and signature and timestamp):
		_deny("missing signing headers")

	client = _load_client(key)
	if not client or not client.is_active:
		_deny("unknown or inactive client")

	_check_timestamp(timestamp)
	_check_signature(client, timestamp, signature)
	_check_ip(client)
	_check_rate_limit(client)

	return client


def assert_can_use(client, project: str | None = None, audience: str | None = None) -> None:
	"""A client sends only through its own project, only to granted audiences."""
	if project and project != client.fcm_project:
		_deny("project not permitted for this client")

	if audience is not None:
		allowed = {row.audience for row in client.allowed_audiences or []}
		if audience not in allowed:
			# Empty means none, not all: granting nothing must not grant everything.
			_deny("audience not permitted for this client")


def _header(name: str) -> str | None:
	if not getattr(frappe.local, "request", None):
		return None
	return frappe.request.headers.get(name)


def _load_client(key: str):
	name = frappe.db.get_value("Push API Client", {"api_key": key}, "name")
	if not name:
		return None
	return frappe.get_cached_doc("Push API Client", name)


def _check_timestamp(timestamp: str) -> None:
	from frappe.utils import now_datetime

	try:
		skew = abs(now_datetime().timestamp() - float(timestamp))
	except (TypeError, ValueError):
		_deny("malformed timestamp")

	if skew > MAX_CLOCK_SKEW_SEC:
		_deny("timestamp outside the replay window")


def _check_signature(client, timestamp: str, signature: str) -> None:
	secret = client.get_password("api_secret", raise_exception=False)
	if not secret:
		_deny("client has no secret configured")

	body = frappe.request.get_data(as_text=True) if getattr(frappe.local, "request", None) else ""
	expected = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), sha256).hexdigest()

	# compare_digest, not ==, so a wrong signature cannot be recovered byte by byte.
	if not hmac.compare_digest(expected, signature):
		_deny("signature mismatch")


def _check_ip(client) -> None:
	allowed = [line.strip() for line in (client.allowed_ips or "").splitlines() if line.strip()]
	if not allowed:
		return

	source = frappe.local.request_ip
	try:
		address = ipaddress.ip_address(source)
	except ValueError:
		_deny("unresolvable source address")

	for entry in allowed:
		try:
			if address in ipaddress.ip_network(entry, strict=False):
				return
		except ValueError:
			continue

	_deny("source address not allowed")


def _check_rate_limit(client) -> None:
	limit = cint(client.rate_limit_per_minute) or 60
	key = frappe.cache.make_key(f"fcm:api_rate:{client.name}:{int(frappe.utils.now_datetime().timestamp() // 60)}")

	used = frappe.cache.incrby(key, 1)
	if used == 1:
		frappe.cache.expire(key, 120)

	if used > limit:
		frappe.throw(_("Rate limit exceeded."), frappe.TooManyRequestsError)


def _deny(reason: str):
	"""One aggregated log line, one generic response.

	The caller learns that it failed, not which check failed - otherwise this is an
	oracle for probing keys, addresses and audience grants.
	"""
	frappe.logger("frappe_fcm_notification").warning(f"api auth denied: {reason}")
	raise frappe.PermissionError(_("Not permitted."))
