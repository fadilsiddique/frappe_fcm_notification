# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Build FCM HTTP v1 message bodies.

Everything client-specific - notification sound, Android channel, APNS bundle id -
comes from the project config. This module holds no constants belonging to any one
customer.
"""

import json

import frappe
from frappe import _
from frappe.utils import strip_html_tags
from frappe.utils.html_utils import unescape_html

# FCM rejects anything larger. Measured on the serialised message, which is what the
# 4KB limit applies to.
MAX_MESSAGE_BYTES = 4096


def build_message(
	*,
	token: str | None = None,
	topic: str | None = None,
	condition: str | None = None,
	title: str,
	body: str,
	data: dict | None = None,
	image: str | None = None,
	project_config=None,
) -> dict:
	"""Return the `message` object for POST /v1/projects/{id}/messages:send."""
	targets = [t for t in (token, topic, condition) if t]
	if len(targets) != 1:
		frappe.throw(_("Exactly one of token, topic or condition is required."))

	config = project_config or frappe._dict()
	title = clean_text(title)
	body = clean_text(body)

	if not title and not body:
		frappe.throw(_("A notification needs a title or a body."))

	message = {"notification": {"title": title, "body": body}}

	if token:
		message["token"] = token
	elif topic:
		message["topic"] = topic
	else:
		message["condition"] = condition

	if image:
		message["notification"]["image"] = image

	android_notification = {}
	if config.get("default_sound"):
		android_notification["sound"] = config.get("default_sound")
	if config.get("android_channel_id"):
		android_notification["channel_id"] = config.get("android_channel_id")
	if android_notification:
		message["android"] = {"notification": android_notification}

	aps = {}
	if config.get("default_sound"):
		aps["sound"] = config.get("default_sound")
	if aps:
		message["apns"] = {"payload": {"aps": aps}}
	if config.get("apns_bundle_id"):
		message.setdefault("apns", {}).setdefault("headers", {})["apns-topic"] = config.get(
			"apns_bundle_id"
		)

	if data:
		message["data"] = coerce_data(data)

	assert_within_size_limit(message)
	return message


def clean_text(value) -> str:
	"""Strip markup from a notification string.

	Two steps, because neither is enough alone: strip_html_tags removes the tags but
	leaves entities, so "Total&nbsp;is 10 &amp; rising" would otherwise reach the device
	verbatim. unescape_html then turns those back into characters.
	"""
	if not value:
		return ""
	return unescape_html(strip_html_tags(str(value))).replace("\xa0", " ").strip()


def coerce_data(data: dict) -> dict[str, str]:
	"""FCM data payloads are string-to-string. Anything else is rejected by the API.

	Nested structures are serialised, None values dropped, everything else stringified.
	"""
	coerced = {}
	for key, value in (data or {}).items():
		if value is None:
			continue
		if isinstance(value, str):
			coerced[str(key)] = value
		elif isinstance(value, dict | list | tuple):
			coerced[str(key)] = json.dumps(value, default=str)
		elif isinstance(value, bool):
			coerced[str(key)] = "true" if value else "false"
		else:
			coerced[str(key)] = str(value)
	return coerced


def assert_within_size_limit(message: dict) -> None:
	size = len(json.dumps(message, default=str).encode("utf-8"))
	if size <= MAX_MESSAGE_BYTES:
		return

	frappe.throw(
		_("FCM message is {0} bytes, over the {1} byte limit. Largest field: {2}.").format(
			size, MAX_MESSAGE_BYTES, _largest_field(message)
		)
	)


def _largest_field(message: dict) -> str:
	"""Name the field to shorten, rather than making the caller guess."""
	sizes = {}
	for key, value in message.items():
		sizes[key] = len(json.dumps(value, default=str).encode("utf-8"))

	if message.get("data"):
		for key, value in message["data"].items():
			sizes[f"data.{key}"] = len(str(value).encode("utf-8"))
		sizes.pop("data", None)

	if not sizes:
		return "unknown"

	name, size = max(sizes.items(), key=lambda item: item[1])
	return f"{name} ({size} bytes)"
