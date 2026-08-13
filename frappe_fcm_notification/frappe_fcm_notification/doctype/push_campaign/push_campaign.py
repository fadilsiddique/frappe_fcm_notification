# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

TERMINAL_STATUSES = ("Completed", "Failed", "Cancelled")


class PushCampaign(Document):
	def validate(self):
		self.validate_audience_project()
		self.validate_callback_url()

	def validate_audience_project(self):
		audience_project = frappe.db.get_value("Push Audience", self.audience, "fcm_project")
		if audience_project and audience_project != self.fcm_project:
			frappe.throw(
				_("Audience {0} belongs to project {1}, not {2}.").format(
					self.audience, audience_project, self.fcm_project
				)
			)

	def validate_callback_url(self):
		"""A caller-supplied URL that the server fetches is an SSRF primitive."""
		if not self.callback_url:
			return

		from frappe_fcm_notification.engine.webhooks import assert_safe_callback_url

		assert_safe_callback_url(self.callback_url)

	def on_submit(self):
		from frappe_fcm_notification.engine.dispatch import dispatch_campaign

		self.db_set("status", "Queued")

		if self.scheduled_at and self.scheduled_at > now_datetime():
			# run_scheduled_campaigns picks it up when it falls due.
			return

		frappe.enqueue(
			dispatch_campaign,
			queue="long",
			timeout=3600,
			enqueue_after_commit=True,
			deduplicate=True,
			job_id=f"push-campaign-{self.name}",
			campaign_name=self.name,
		)

	def on_cancel(self):
		"""Running batches see this at their next status check and stop."""
		self.db_set("status", "Cancelled")
