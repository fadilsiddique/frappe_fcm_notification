# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Regression guard for the credential disclosure fixed in 0.1.0.

Versions up to 0.0.1 whitelisted four functions - one of which returned the decrypted
service account private key, another a live OAuth bearer token - and exposed a fifth
with allow_guest=True. This walks the AST of every module in the app and fails if any
of that comes back.

Do not skip, xfail, or narrow this test.
"""

import ast
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent

# The single file permitted to expose HTTP endpoints.
API_MODULE = APP_ROOT / "api" / "v1.py"

# Names that must not exist anywhere: they were the vulnerability, and a caller that can
# still import them can still re-expose them.
FORBIDDEN_NAMES = {"get_fcm_credentials", "get_cached_access_token", "get_ios_device_token"}


def python_files():
	for path in APP_ROOT.rglob("*.py"):
		if "__pycache__" in path.parts:
			continue
		yield path


def _dotted(node) -> str:
	parts = []
	while isinstance(node, ast.Attribute):
		parts.append(node.attr)
		node = node.value
	if isinstance(node, ast.Name):
		parts.append(node.id)
	return ".".join(reversed(parts))


def decorator_names(node):
	"""Every dotted decorator name on a function, e.g. frappe.whitelist."""
	for decorator in node.decorator_list:
		call = decorator.func if isinstance(decorator, ast.Call) else decorator
		parts = []
		while isinstance(call, ast.Attribute):
			parts.append(call.attr)
			call = call.value
		if isinstance(call, ast.Name):
			parts.append(call.id)
		if parts:
			yield ".".join(reversed(parts)), decorator


class TestSecuritySurface(unittest.TestCase):
	def test_whitelist_only_in_api_v1(self):
		offenders = []

		for path in python_files():
			tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
			for node in ast.walk(tree):
				if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
					continue
				for name, _decorator in decorator_names(node):
					if name.endswith("whitelist") and path != API_MODULE:
						offenders.append(f"{path.relative_to(APP_ROOT)}:{node.lineno} {node.name}")

		self.assertEqual(
			offenders,
			[],
			"@frappe.whitelist() is only allowed in api/v1.py. Offenders: " + ", ".join(offenders),
		)

	def test_no_allow_guest_anywhere(self):
		offenders = []

		for path in python_files():
			tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
			for node in ast.walk(tree):
				if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
					continue
				for _name, decorator in decorator_names(node):
					if not isinstance(decorator, ast.Call):
						continue
					for keyword in decorator.keywords:
						if keyword.arg == "allow_guest" and getattr(keyword.value, "value", False):
							offenders.append(
								f"{path.relative_to(APP_ROOT)}:{node.lineno} {node.name}"
							)

		self.assertEqual(
			offenders, [], "allow_guest is never permitted in this app. Offenders: " + ", ".join(offenders)
		)

	def test_credential_functions_are_gone(self):
		offenders = []

		for path in python_files():
			if path.parts[-2:] == ("tests", "test_security.py"):
				continue
			tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
			for node in ast.walk(tree):
				if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in FORBIDDEN_NAMES:
					offenders.append(f"{path.relative_to(APP_ROOT)}:{node.lineno} {node.name}")

		self.assertEqual(
			offenders,
			[],
			"These functions were the vulnerability and must not be reintroduced: "
			+ ", ".join(offenders),
		)

	def test_outbound_http_always_has_a_timeout(self):
		"""A call without one parks an RQ worker on a hung connection, indefinitely."""
		http_methods = {"post", "get", "put", "patch", "delete", "request"}
		offenders = []

		for path in python_files():
			tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
			for node in ast.walk(tree):
				if not isinstance(node, ast.Call):
					continue

				name = _dotted(node.func)
				head, _, method = name.rpartition(".")
				if method not in http_methods:
					continue
				if not (head.endswith("requests") or head.endswith("session")):
					continue

				if not any(keyword.arg == "timeout" for keyword in node.keywords):
					offenders.append(f"{path.relative_to(APP_ROOT)}:{node.lineno} {name}")

		self.assertEqual(offenders, [], "outbound HTTP without a timeout: " + ", ".join(offenders))

	def test_api_v1_endpoints_are_authenticated(self):
		"""Every whitelisted function in api/v1.py must be reachable only when logged in."""
		tree = ast.parse(API_MODULE.read_text(encoding="utf-8"), filename=str(API_MODULE))
		endpoints = []

		for node in ast.walk(tree):
			if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
				continue
			for name, decorator in decorator_names(node):
				if not name.endswith("whitelist"):
					continue
				endpoints.append(node.name)
				if isinstance(decorator, ast.Call):
					for keyword in decorator.keywords:
						self.assertNotEqual(
							keyword.arg,
							"allow_guest",
							f"{node.name} must not set allow_guest",
						)

		self.assertTrue(endpoints, "api/v1.py should expose at least one endpoint")
