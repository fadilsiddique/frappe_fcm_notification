#!/usr/bin/env python3
"""Fail the build if the app's security invariants regress.

Runs without a bench, so it gates every push before the suite is even installed. The
same rules are asserted by frappe_fcm_notification/tests/test_security.py; this is the
copy that runs when there is no site.

Invariants:
  1. @frappe.whitelist appears only in api/v1.py
  2. allow_guest is never passed anywhere
  3. every outbound HTTP call passes a timeout
  4. the functions that were the 0.0.1 credential disclosure stay deleted
"""

import ast
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "frappe_fcm_notification"
API_MODULE = APP / "api" / "v1.py"
TESTS = APP / "tests"

FORBIDDEN_NAMES = {"get_fcm_credentials", "get_cached_access_token", "get_ios_device_token"}
HTTP_METHODS = {"post", "get", "put", "patch", "delete", "request"}

failures: list[str] = []


def dotted(node) -> str:
	parts = []
	while isinstance(node, ast.Attribute):
		parts.append(node.attr)
		node = node.value
	if isinstance(node, ast.Name):
		parts.append(node.id)
	return ".".join(reversed(parts))


def check(path: Path) -> None:
	rel = path.relative_to(APP.parent)
	tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

	for node in ast.walk(tree):
		if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
			_check_function(node, path, rel)
		elif isinstance(node, ast.Call):
			_check_call(node, rel)


def _check_function(node, path: Path, rel: Path) -> None:
	if node.name in FORBIDDEN_NAMES and TESTS not in path.parents:
		failures.append(f"{rel}:{node.lineno} {node.name}() was the 0.0.1 disclosure; keep it deleted")

	for decorator in node.decorator_list:
		call = decorator.func if isinstance(decorator, ast.Call) else decorator
		name = dotted(call)

		if name.endswith("whitelist") and path != API_MODULE:
			failures.append(f"{rel}:{node.lineno} @{name} is only allowed in api/v1.py")

		if isinstance(decorator, ast.Call):
			for keyword in decorator.keywords:
				if keyword.arg == "allow_guest":
					failures.append(f"{rel}:{node.lineno} allow_guest is never permitted")


def _check_call(node, rel: Path) -> None:
	name = dotted(node.func)
	if not name:
		return

	head, _, method = name.rpartition(".")
	if method not in HTTP_METHODS:
		return
	if not (head.endswith("requests") or head.endswith("session") or head.endswith("_session")):
		return

	if not any(keyword.arg == "timeout" for keyword in node.keywords):
		failures.append(f"{rel}:{node.lineno} {name}() has no timeout; a hung call parks a worker")


def main() -> int:
	for path in sorted(APP.rglob("*.py")):
		if "__pycache__" in path.parts:
			continue
		check(path)

	if failures:
		print("Security surface check failed:\n")
		for failure in failures:
			print(f"  {failure}")
		return 1

	print("Security surface check passed.")
	return 0


if __name__ == "__main__":
	sys.exit(main())
