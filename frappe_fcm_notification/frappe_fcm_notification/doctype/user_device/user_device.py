# Copyright (c) 2024, Upscape Technologies and contributors
# For license information, please see license.txt

import frappe
import requests
import json
from frappe.model.document import Document
from frappe_fcm_notification.fcm_notification import get_cached_access_token


class UserDevice(Document):
	
	def after_insert(self):

		if self.device_type == "ios":
			self.device_token = get_ios_device_token(self.device_token)
			self.save()

@frappe.whitelist(allow_guest=True)
def get_ios_device_token(device_token):
	access_token = get_cached_access_token()

	url = "https://iid.googleapis.com/iid/v1:batchImport"

	ios_application_id = frappe.db.get_single_value("FCM Notification Settings", "ios_application_id") or "com.upscape.crm"

	payload = json.dumps({
		"application": ios_application_id,
		"sandbox":False,
		"apns_tokens":[device_token]
	})

	headers = {
		'access_token_auth': 'true',
		'Content-Type': 'application/json',
		'Authorization': f'Bearer {access_token["access_token"]}'
	}

	response = requests.post(url, headers=headers, data=payload)
	response_data = response.json()

	if response.status_code == 200:
		for result in response_data.get("results", []):
			if result.get("status") == "OK":
				return result.get("registration_token")
			else:
				frappe.log_error(title="FCM Token Error", message=f"Error processing token: {result}")
	else:
		frappe.log_error(title="FCM API Error", message=response.text)



