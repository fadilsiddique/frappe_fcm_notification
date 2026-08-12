# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Per-project service account loading, token caching and HTTP sessions.

Nothing here writes to the database. The old implementation cached the OAuth token on
a Single doctype, which meant a save() and a commit() on the send path - inside a
doc_events hook, committing the caller's partial transaction - plus row-lock contention
on tabSingles across every worker. The cache lives in Redis now.

Never log the token, the credentials object, or the service account JSON.
"""

import json

import frappe
from frappe import _
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2 import service_account
from redis.exceptions import LockError
from requests.adapters import HTTPAdapter

FCM_SCOPES = ["https://www.googleapis.com/auth/firebase.messaging"]

# Google mints one-hour tokens; expire ours slightly early so a send never races the
# expiry.
TOKEN_TTL_SEC = 3300
LOCK_TIMEOUT_SEC = 30
LOCK_WAIT_SEC = 10

# Per-process caches. Keyed by project, each entry tagged with the credential version so
# a change in one process invalidates every other process's copy (a doc hook alone only
# ever reaches the process that saved the document).
_CREDENTIALS: dict[str, tuple[int, dict]] = {}
_SESSIONS: dict[str, tuple[int, AuthorizedSession]] = {}


def resolve_project(name: str | None = None) -> str:
	"""Return an explicit project name, or the default one."""
	if name:
		return name

	project = frappe.db.get_value("FCM Project", {"is_default": 1, "is_active": 1}, "name")
	if not project:
		frappe.throw(_("No default FCM Project is configured."), frappe.DoesNotExistError)

	return project


def get_project_config(project: str | None = None) -> frappe._dict:
	"""Delivery settings for a project. Never includes credentials."""
	doc = frappe.get_cached_doc("FCM Project", resolve_project(project))
	return frappe._dict(
		name=doc.name,
		firebase_project_id=doc.firebase_project_id,
		default_sound=doc.default_sound,
		android_channel_id=doc.android_channel_id,
		apns_bundle_id=doc.apns_bundle_id,
		rate_limit_per_second=doc.rate_limit_per_second,
		throttle_max_block_sec=doc.throttle_max_block_sec,
		success_sample_rate=doc.success_sample_rate,
	)


def parse_service_account(raw: str | dict) -> dict:
	"""Parse and sanity-check a service account JSON blob.

	Called from FCM Project.validate too: a malformed key should fail at configuration
	time, not at 3am during a send.
	"""
	if not raw:
		frappe.throw(_("No service account credentials configured."))

	info = raw if isinstance(raw, dict) else json.loads(raw)

	if info.get("type") != "service_account":
		frappe.throw(_("Not a service account key: expected type 'service_account'."))

	if not info.get("private_key"):
		frappe.throw(_("Service account JSON has no private_key."))

	if not info.get("project_id"):
		frappe.throw(_("Service account JSON has no project_id."))

	# Keys pasted through a form arrive with literal backslash-n instead of newlines.
	info["private_key"] = info["private_key"].replace("\\n", "\n").strip()

	return info


def get_service_account_info(project: str | None = None) -> dict:
	project = resolve_project(project)
	version = _credential_version(project)

	cached = _CREDENTIALS.get(project)
	if cached and cached[0] == version:
		return cached[1]

	info = _load_service_account_info(project)
	_CREDENTIALS[project] = (version, info)
	return info


def get_access_token(project: str | None = None) -> str:
	"""Return a cached OAuth token, minting one under a lock if the cache is cold.

	The lock stops twenty workers stampeding Google's token endpoint on a cold cache.
	It has to be prefixed by hand: frappe.cache.lock() comes from redis-py, not from
	Frappe, so it does not apply the per-site key prefix that set_value/get_value do,
	and every site on this Redis would otherwise share one lock.
	"""
	project = resolve_project(project)
	cache_key = f"fcm:token:{project}"

	token = frappe.cache.get_value(cache_key)
	if token:
		return token

	lock_key = frappe.cache.make_key(f"fcm:token:lock:{project}")
	try:
		with frappe.cache.lock(lock_key, timeout=LOCK_TIMEOUT_SEC, blocking_timeout=LOCK_WAIT_SEC):
			token = frappe.cache.get_value(cache_key)
			if not token:
				token = _mint_access_token(project)
				frappe.cache.set_value(cache_key, token, expires_in_sec=TOKEN_TTL_SEC)
			return token
	except LockError:
		# Someone else is holding the lock and taking their time. Mint an extra token
		# rather than failing the send - worst case Google issues two.
		token = _mint_access_token(project)
		frappe.cache.set_value(cache_key, token, expires_in_sec=TOKEN_TTL_SEC)
		return token


def get_authorized_session(project: str | None = None) -> AuthorizedSession:
	"""A pooled, credential-backed session, reused for the life of the process.

	max_retries=0 is deliberate: retries are decided by fcm.errors.classify, which knows
	the difference between a dead token and a busy server. urllib3 does not.
	"""
	project = resolve_project(project)
	version = _credential_version(project)

	cached = _SESSIONS.get(project)
	if cached and cached[0] == version:
		return cached[1]

	session = AuthorizedSession(_google_credentials(project))
	session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=20, max_retries=0))
	_SESSIONS[project] = (version, session)
	return session


def invalidate(project: str) -> None:
	"""Drop every cached copy of a project's credentials, in every process."""
	frappe.cache.set_value(f"fcm:cred_version:{project}", _credential_version(project) + 1)
	frappe.cache.delete_value(f"fcm:token:{project}")
	_CREDENTIALS.pop(project, None)
	_SESSIONS.pop(project, None)


def on_project_update(doc, method=None):
	"""doc_events hook on FCM Project."""
	invalidate(doc.name)


def _credential_version(project: str) -> int:
	version = frappe.cache.get_value(f"fcm:cred_version:{project}")
	if version is None:
		version = 1
		frappe.cache.set_value(f"fcm:cred_version:{project}", version)
	return int(version)


def _load_service_account_info(project: str) -> dict:
	doc = frappe.get_cached_doc("FCM Project", project)

	if doc.credentials_source == "Site Config":
		raw = frappe.conf.get(f"fcm_service_account_{doc.name}")
		if not raw:
			frappe.throw(
				_("site_config.json has no key {0}.").format(f"fcm_service_account_{doc.name}")
			)
	else:
		raw = doc.get_password("service_account_json")

	return parse_service_account(raw)


def _google_credentials(project: str):
	return service_account.Credentials.from_service_account_info(
		get_service_account_info(project), scopes=FCM_SCOPES
	)


def _mint_access_token(project: str) -> str:
	credentials = _google_credentials(project)
	credentials.refresh(Request())
	return credentials.token
