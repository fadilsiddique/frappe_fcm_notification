# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class PushAPIClient(Document):
	def before_insert(self):
		if not self.api_key:
			self.api_key = frappe.generate_hash(length=32)
		if not self.api_secret:
			self.api_secret = frappe.generate_hash(length=48)

	def validate(self):
		self.validate_audience_projects()

	def validate_audience_projects(self):
		"""A client is scoped to one project; its audiences must live in that project.

		Without this, granting an audience would quietly widen a client's reach beyond
		the project it was issued for.
		"""
		for row in self.allowed_audiences or []:
			project = frappe.db.get_value("Push Audience", row.audience, "fcm_project")
			if project and project != self.fcm_project:
				frappe.throw(
					_("Audience {0} belongs to project {1}, but this client is scoped to {2}.").format(
						row.audience, project, self.fcm_project
					)
				)

	def on_update(self):
		frappe.cache.delete_value(f"fcm:api_client:{self.api_key}")

	def on_trash(self):
		frappe.cache.delete_value(f"fcm:api_client:{self.api_key}")
