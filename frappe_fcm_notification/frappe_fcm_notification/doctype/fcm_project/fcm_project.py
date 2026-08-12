# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from frappe_fcm_notification.fcm.credentials import invalidate, parse_service_account


class FCMProject(Document):
	def validate(self):
		self.validate_credentials()
		self.validate_single_default()

	def validate_credentials(self):
		"""Fail at configuration time rather than at 3am during a send."""
		if self.credentials_source != "DocType":
			return

		raw = self.service_account_json
		if not raw and not self.is_new():
			# Password fields come back masked on an unchanged document.
			raw = self.get_password("service_account_json", raise_exception=False)

		if not raw:
			return

		info = parse_service_account(raw)

		if not self.firebase_project_id:
			self.firebase_project_id = info["project_id"]
		elif self.firebase_project_id != info["project_id"]:
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
		invalidate(self.name)

	def on_trash(self):
		invalidate(self.name)
