# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document

from frappe_fcm_notification.fcm.credentials import invalidate, parse_service_account


class FCMProject(Document):
	def validate(self):
		self.ingest_credentials()
		self.validate_credentials()
		self.validate_single_default()

	def ingest_credentials(self):
		"""Move an uploaded or pasted key into the encrypted field.

		Neither input field is ever written to the database: both are cleared here,
		before the row is saved, and the key itself only lands in `__Auth` where Frappe
		encrypts Password fields. The upload is deleted in on_update, because a service
		account key sitting in /private/files is a plaintext credential on disk.
		"""
		raw = None

		if self.service_account_file:
			raw = self.read_uploaded_key()
		elif self.service_account_paste:
			raw = self.service_account_paste

		if not raw:
			return

		info = parse_service_account(raw)

		self.service_account_json = json.dumps(info)
		self.service_account_paste = None
		self.credentials_summary = _("{0} · key {1}…").format(
			info.get("client_email") or _("unknown"), (info.get("private_key_id") or "")[:8]
		)

		if not self.firebase_project_id:
			self.firebase_project_id = info["project_id"]

	def read_uploaded_key(self) -> str:
		file_name = frappe.db.get_value("File", {"file_url": self.service_account_file}, "name")
		if not file_name:
			frappe.throw(_("Could not read the uploaded service account file."))

		file_doc = frappe.get_doc("File", file_name)
		content = file_doc.get_content()

		# Remember it for on_update; the row must be saved before the attachment goes.
		self.flags.uploaded_key_file = file_name
		self.service_account_file = None

		return content.decode("utf-8") if isinstance(content, bytes) else content

	def validate_credentials(self):
		"""Fail at configuration time rather than at 3am during a send."""
		if self.credentials_source != "DocType":
			return

		raw = self.service_account_json
		if not raw:
			if not self.is_new():
				frappe.throw(_("Upload or paste the service account JSON."))
			return

		# A loaded document carries '****' in place of the stored value; it was validated
		# when it was set, and re-parsing the mask would fail.
		if self.is_dummy_password(raw):
			return

		info = parse_service_account(raw)

		if self.firebase_project_id != info["project_id"]:
			# Sending with a key from a different project is the usual cause of
			# SENDER_ID_MISMATCH, which silently kills every token.
			frappe.throw(
				_("Firebase Project Id {0} does not match the service account project {1}.").format(
					self.firebase_project_id, info["project_id"]
				)
			)

	def validate_single_default(self):
		"""Exactly one default, always. Sends that do not name a project depend on it."""
		another_default = frappe.db.exists(
			"FCM Project", {"is_default": 1, "name": ("!=", self.name)}
		)

		if not self.is_default:
			# Never leave the site without a default - the first project becomes it.
			if not another_default:
				self.is_default = 1
			return

		for other in frappe.get_all(
			"FCM Project", filters={"is_default": 1, "name": ("!=", self.name)}, pluck="name"
		):
			frappe.db.set_value("FCM Project", other, "is_default", 0, update_modified=False)

	def on_update(self):
		self.discard_uploaded_key()
		invalidate(self.name)

	def discard_uploaded_key(self):
		"""The key is encrypted in __Auth now; the uploaded copy must not linger."""
		file_name = self.flags.pop("uploaded_key_file", None)
		if not file_name:
			return

		frappe.delete_doc("File", file_name, force=True, ignore_permissions=True, delete_permanently=True)

	def on_trash(self):
		invalidate(self.name)
