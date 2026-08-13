# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document

# FCM's permitted topic characters.
TOPIC_PATTERN = re.compile(r"^[a-zA-Z0-9\-_.~%]+$")

REQUIRED_FIELD = {
	"Topic": "topic",
	"Role": "role",
	"User Group": "user_group",
	"Report": "report",
	"Method": "resolver_method",
}


class PushAudience(Document):
	def validate(self):
		self.validate_selector()
		self.validate_topic()
		self.validate_resolver()

	def validate_selector(self):
		field = REQUIRED_FIELD.get(self.audience_type)
		if field and not self.get(field):
			frappe.throw(_("{0} is required for a {1} audience.").format(_(field), self.audience_type))

		if self.audience_type == "User List" and not self.users:
			frappe.throw(_("Add at least one user to a User List audience."))

	def validate_topic(self):
		if self.topic and not TOPIC_PATTERN.match(self.topic):
			frappe.throw(
				_("Topic names may only contain letters, digits and - _ . ~ % characters.")
			)

	def validate_resolver(self):
		"""A dotted path from a document is arbitrary code execution unless allowlisted.

		Anyone who can write a Push Audience could otherwise point resolver_method at any
		callable in the environment, so the path has to be declared by installed app code.
		"""
		if self.audience_type != "Method":
			return

		allowed = frappe.get_hooks("fcm_audience_resolvers") or []
		if self.resolver_method not in allowed:
			frappe.throw(
				_(
					"{0} is not an allowed resolver. Add it to the fcm_audience_resolvers hook "
					"of an installed app first."
				).format(self.resolver_method)
			)
