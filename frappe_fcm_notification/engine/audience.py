# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Turn a Push Audience into either a topic name or a stream of device tokens.

The token side is always a generator. Materialising a 200k-row audience in a worker is
an out-of-memory kill, and the coordinator only ever needs one chunk at a time.
"""

from collections.abc import Iterator
from enum import StrEnum

import frappe
from frappe import _

PAGE_LENGTH = 1000

# Topics maintained automatically at device registration. An audience that maps onto one
# of these can be served by a single API call instead of one call per token.
DERIVED_TOPICS = {"All": "all"}


class Strategy(StrEnum):
	TOPIC = "Topic"
	TOKEN = "Token"


def resolve(audience_doc, project: str) -> tuple[Strategy, str | Iterator[str]]:
	"""Return (Strategy.TOPIC, topic_name) or (Strategy.TOKEN, token_generator)."""
	strategy = choose_strategy(audience_doc)

	if strategy == Strategy.TOPIC:
		return strategy, topic_for(audience_doc)

	return strategy, iter_tokens(audience_doc, project)


def choose_strategy(audience_doc) -> Strategy:
	hint = audience_doc.get("strategy_hint") or "Auto"

	if hint == "Force Topic":
		if not topic_for(audience_doc):
			frappe.throw(_("This audience has no topic to send to."))
		return Strategy.TOPIC

	if hint == "Force Token":
		return Strategy.TOKEN

	return Strategy.TOPIC if topic_for(audience_doc) else Strategy.TOKEN


def topic_for(audience_doc) -> str | None:
	if audience_doc.audience_type == "Topic":
		return audience_doc.topic
	return DERIVED_TOPICS.get(audience_doc.audience_type)


def iter_tokens(audience_doc, project: str) -> Iterator[str]:
	"""Stream active device tokens for an audience, a page at a time."""
	audience_type = audience_doc.audience_type

	if audience_type in ("All", "Topic"):
		yield from _tokens_for_filters({}, project)

	elif audience_type == "Role":
		yield from _tokens_for_users(_users_with_role(audience_doc.role), project)

	elif audience_type == "User Group":
		yield from _tokens_for_users(_users_in_group(audience_doc.user_group), project)

	elif audience_type == "User List":
		yield from _tokens_for_users(
			(row.user for row in audience_doc.users or []), project
		)

	elif audience_type == "Report":
		yield from _tokens_from_report(audience_doc.report, project)

	elif audience_type == "Method":
		yield from _tokens_from_method(audience_doc.resolver_method, audience_doc, project)

	else:
		frappe.throw(_("Unsupported audience type {0}.").format(audience_type))


def _tokens_for_filters(filters: dict, project: str) -> Iterator[str]:
	offset = 0
	base = {"is_active": 1, "fcm_project": project, **filters}

	while True:
		tokens = frappe.get_all(
			"Push Device",
			filters=base,
			pluck="token",
			order_by="name",
			offset=offset,
			limit_page_length=PAGE_LENGTH,
		)
		if not tokens:
			return
		yield from tokens
		offset += PAGE_LENGTH


def _tokens_for_users(users, project: str) -> Iterator[str]:
	"""Page over users too - an IN clause with 50k entries is its own problem."""
	chunk: list[str] = []

	for user in users:
		if not user:
			continue
		chunk.append(user)
		if len(chunk) >= 200:
			yield from _tokens_for_filters({"user": ("in", chunk)}, project)
			chunk = []

	if chunk:
		yield from _tokens_for_filters({"user": ("in", chunk)}, project)


def _users_with_role(role: str) -> Iterator[str]:
	offset = 0
	while True:
		rows = frappe.get_all(
			"Has Role",
			filters={"role": role, "parenttype": "User"},
			pluck="parent",
			offset=offset,
			limit_page_length=PAGE_LENGTH,
		)
		if not rows:
			return
		yield from rows
		offset += PAGE_LENGTH


def _users_in_group(user_group: str) -> Iterator[str]:
	yield from frappe.get_all(
		"User Group Member", filters={"parent": user_group}, pluck="user", limit_page_length=0
	)


def _tokens_from_report(report: str, project: str) -> Iterator[str]:
	"""Run a Query Report and take its user or token column.

	Runs with the caller's permissions: a report is a query, and this must not become a
	way to read rows the campaign owner cannot see.
	"""
	from frappe.desk.query_report import run

	result = run(report, ignore_prepared_report=True, are_default_filters=False)
	columns = [c.get("fieldname") or c.get("label") for c in result.get("columns") or []]
	rows = result.get("result") or []

	if "token" in columns:
		index = columns.index("token")
		yield from (_row_value(row, index, "token") for row in rows)
		return

	if "user" not in columns:
		frappe.throw(_("Report {0} must return a user or token column.").format(report))

	index = columns.index("user")
	users = (_row_value(row, index, "user") for row in rows)
	yield from _tokens_for_users(users, project)


def _row_value(row, index: int, key: str):
	if isinstance(row, dict):
		return row.get(key)
	return row[index] if len(row) > index else None


def _tokens_from_method(method: str, audience_doc, project: str) -> Iterator[str]:
	"""Call an allowlisted resolver.

	The allowlist is checked again here, not only in Push Audience.validate: a hook can
	be removed after an audience was saved, and a stale dotted path must stop working
	rather than keep executing.
	"""
	allowed = frappe.get_hooks("fcm_audience_resolvers") or []
	if method not in allowed:
		frappe.throw(_("Resolver {0} is not in the fcm_audience_resolvers allowlist.").format(method))

	result = frappe.get_attr(method)(audience=audience_doc, project=project)
	for value in result or []:
		if value:
			yield value
