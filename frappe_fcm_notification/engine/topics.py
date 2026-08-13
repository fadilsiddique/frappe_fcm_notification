# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Topic subscription management.

A topic broadcast is one API call regardless of audience size, which is the only way to
reach a large audience without paying for concurrency ourselves. The cost is keeping
subscriptions in step with the device table.

subscribed_topics on Push Device is a cache of what succeeded, never the source of
truth; reconcile_topics repairs drift.
"""

import json
import re

import frappe

from frappe_fcm_notification.fcm.client import FCMClient

TOPIC_SLUG_PATTERN = re.compile(r"[^a-zA-Z0-9\-_.~%]+")

# Batch subscribe accepts 1000 tokens per call.
SUBSCRIBE_CHUNK = 1000


def derived_topics(device) -> list[str]:
	"""Topics every device belongs to, derived from what we know about it."""
	topics = ["all"]

	if device.get("platform"):
		topics.append(f"platform_{slug(device.platform)}")

	if device.get("locale"):
		topics.append(f"lang_{slug(device.locale)}")

	for role in _roles_for(device.get("user")):
		topics.append(f"role_{slug(role)}")

	return topics


def slug(value: str) -> str:
	return TOPIC_SLUG_PATTERN.sub("_", (value or "").strip().lower())


def _roles_for(user: str | None) -> list[str]:
	if not user or user == "Guest":
		return []

	return frappe.get_all(
		"Has Role", filters={"parent": user, "parenttype": "User"}, pluck="role", limit_page_length=0
	)


def sync_device_topics(device_name: str) -> None:
	"""Bring one device's subscriptions in line with its derived topics.

	Never raises: a device is registered whether or not Google accepts the subscription,
	and reconcile_topics repairs whatever did not land.
	"""
	try:
		_sync_device_topics(device_name)
	except Exception as e:
		frappe.logger("frappe_fcm_notification").warning(
			f"topic sync for {device_name} failed: {e.__class__.__name__}"
		)


def _sync_device_topics(device_name: str) -> None:
	device = frappe.get_doc("Push Device", device_name)
	if not device.is_active:
		unsubscribe_all(device_name)
		return

	wanted = set(derived_topics(device))
	current = set(json.loads(device.subscribed_topics or "[]"))

	client = FCMClient(device.fcm_project)
	subscribed = set(current)

	for topic in wanted - current:
		if client.subscribe_topic([device.token], topic):
			subscribed.add(topic)

	for topic in current - wanted:
		if client.unsubscribe_topic([device.token], topic):
			subscribed.discard(topic)

	frappe.db.set_value(
		"Push Device",
		device_name,
		"subscribed_topics",
		json.dumps(sorted(subscribed)),
		update_modified=False,
	)


def unsubscribe_all(device_name: str) -> None:
	device = frappe.get_doc("Push Device", device_name)
	current = json.loads(device.subscribed_topics or "[]")
	if not current:
		return

	client = FCMClient(device.fcm_project)
	for topic in current:
		client.unsubscribe_topic([device.token], topic)

	frappe.db.set_value(
		"Push Device", device_name, "subscribed_topics", "[]", update_modified=False
	)


def enqueue_device_sync(doc, method=None) -> None:
	"""doc_events on Push Device. Queued: registration must not wait on Google."""
	if not frappe.db.get_value("FCM Project", doc.fcm_project, "auto_subscribe_topics"):
		# Sites that only send transactional pushes should not pay a job per registration.
		return

	frappe.enqueue(
		sync_device_topics,
		queue="short",
		enqueue_after_commit=True,
		deduplicate=True,
		job_id=f"push-topics-{doc.name}",
		device_name=doc.name,
	)


def reconcile_topics(fcm_project: str | None = None, limit: int = 5000) -> dict:
	"""Nightly repair for devices whose subscriptions drifted.

	Subscription calls fail silently often enough that a device can believe it is on a
	topic it never joined; without this the topic path quietly under-delivers.
	"""
	filters = {"is_active": 1}
	if fcm_project:
		filters["fcm_project"] = fcm_project

	devices = frappe.get_all(
		"Push Device",
		filters=filters,
		fields=["name", "platform", "locale", "user", "subscribed_topics"],
		limit_page_length=limit,
		order_by="modified asc",
	)

	drifted = 0
	for device in devices:
		wanted = set(derived_topics(device))
		current = set(json.loads(device.subscribed_topics or "[]"))
		if wanted != current:
			drifted += 1
			frappe.enqueue(
				sync_device_topics,
				queue="long",
				deduplicate=True,
				job_id=f"push-topics-{device.name}",
				device_name=device.name,
			)

	return {"checked": len(devices), "drifted": drifted}
