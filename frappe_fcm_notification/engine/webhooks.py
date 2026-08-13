# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Completion callbacks.

Signed with the owning API client's secret using the same scheme as inbound requests,
so a receiver can verify the call really came from us. A failing webhook never changes
the campaign's status - the campaign either delivered or it did not, regardless of
whether anyone was listening.
"""

import hmac
import ipaddress
import socket
from hashlib import sha256
from urllib.parse import urlparse

import frappe
import requests
from frappe import _
from frappe.utils import now_datetime

WEBHOOK_TIMEOUT = (3.05, 10)
MAX_ATTEMPTS = 3


def assert_safe_callback_url(url: str) -> None:
	"""Reject URLs that would turn the callback into an SSRF primitive.

	The server fetches this URL, so a caller could otherwise point it at the bench's own
	metadata service, Redis, or a neighbouring site.
	"""
	parsed = urlparse(url)

	if parsed.scheme not in ("http", "https"):
		frappe.throw(_("Callback URL must be http or https."))

	if parsed.scheme == "http" and not frappe.conf.get("developer_mode"):
		frappe.throw(_("Callback URL must use https."))

	host = parsed.hostname
	if not host:
		frappe.throw(_("Callback URL has no host."))

	for address in _resolve(host):
		if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
			if frappe.conf.get("developer_mode"):
				continue
			frappe.throw(_("Callback URL may not point at a private address."))


def _resolve(host: str) -> list:
	try:
		infos = socket.getaddrinfo(host, None)
	except OSError:
		frappe.throw(_("Callback host {0} does not resolve.").format(host))

	addresses = []
	for info in infos:
		try:
			addresses.append(ipaddress.ip_address(info[4][0]))
		except ValueError:
			continue
	return addresses


def enqueue_completion_webhook(campaign_name: str) -> None:
	callback_url = frappe.db.get_value("Push Campaign", campaign_name, "callback_url")
	if not callback_url:
		return

	frappe.enqueue(
		deliver_completion_webhook,
		queue="short",
		enqueue_after_commit=True,
		campaign_name=campaign_name,
	)


def deliver_completion_webhook(campaign_name: str) -> None:
	campaign = frappe.get_doc("Push Campaign", campaign_name)
	if not campaign.callback_url:
		return

	body = frappe.as_json(
		{
			"campaign_id": campaign.name,
			"status": campaign.status,
			"strategy": campaign.strategy,
			"total_recipients": campaign.total_recipients,
			"sent_count": campaign.sent_count,
			"failed_count": campaign.failed_count,
			"invalid_count": campaign.invalid_count,
			"started_at": str(campaign.started_at or ""),
			"completed_at": str(campaign.completed_at or ""),
		}
	)

	headers = {"Content-Type": "application/json"}
	secret = _client_secret(campaign.api_client)
	if secret:
		timestamp = str(int(now_datetime().timestamp()))
		headers["X-Push-Timestamp"] = timestamp
		headers["X-Push-Signature"] = sign(secret, timestamp, body)

	for attempt in range(1, MAX_ATTEMPTS + 1):
		try:
			response = requests.post(
				campaign.callback_url, data=body, headers=headers, timeout=WEBHOOK_TIMEOUT
			)
			if response.status_code < 400:
				return
			error = f"HTTP {response.status_code}"
		except requests.RequestException as e:
			error = e.__class__.__name__

		frappe.logger("frappe_fcm_notification").warning(
			f"webhook attempt {attempt}/{MAX_ATTEMPTS} for {campaign_name} failed: {error}"
		)

	frappe.logger("frappe_fcm_notification").error(
		f"webhook for {campaign_name} gave up after {MAX_ATTEMPTS} attempts"
	)


def sign(secret: str, timestamp: str, body: str) -> str:
	"""HMAC-SHA256 over "{timestamp}.{body}" - the same scheme inbound requests use."""
	message = f"{timestamp}.{body}".encode()
	return hmac.new(secret.encode(), message, sha256).hexdigest()


def _client_secret(api_client: str | None) -> str | None:
	if not api_client:
		return None

	return frappe.utils.password.get_decrypted_password(
		"Push API Client", api_client, "api_secret", raise_exception=False
	)
