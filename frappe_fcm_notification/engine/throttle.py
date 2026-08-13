# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Redis token bucket, shared by every worker sending for a project.

The wait is bounded on purpose. Blocking indefinitely inside an RQ worker to respect a
rate limit parks the worker - the same failure mode as an HTTP call without a timeout.
When the bucket stays empty the batch is requeued instead.
"""

import time

import frappe

# Refill granularity. Finer buckets smooth bursts but cost a Redis round trip each.
WINDOW_SEC = 1


class ThrottleTimeout(Exception):
	"""The rate limit could not be satisfied within the caller's budget."""


def consume(project: str, count: int = 1, max_block_sec: float = 5.0) -> None:
	"""Reserve `count` sends, waiting up to max_block_sec.

	Raises ThrottleTimeout so the caller can requeue rather than sleep on.
	"""
	limit = _limit_for(project)
	if not limit:
		return

	deadline = time.monotonic() + max_block_sec

	while True:
		if _try_consume(project, count, limit):
			return

		if time.monotonic() >= deadline:
			raise ThrottleTimeout(
				f"rate limit for {project} not satisfied within {max_block_sec}s"
			)

		time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _try_consume(project: str, count: int, limit: int) -> bool:
	"""One INCRBY per window, expiring on its own. No Lua, no lock, no cleanup job."""
	window = int(time.time() // WINDOW_SEC)
	key = frappe.cache.make_key(f"fcm:throttle:{project}:{window}")

	used = frappe.cache.incrby(key, count)
	if used == count:
		# First writer in this window sets the expiry.
		frappe.cache.expire(key, WINDOW_SEC * 3)

	if used <= limit:
		return True

	# Give the reservation back so a competing worker is not starved by our overshoot.
	frappe.cache.incrby(key, -count)
	return False


def _limit_for(project: str) -> int:
	limit = frappe.db.get_value("FCM Project", project, "rate_limit_per_second")
	return int(limit or 0)
