import requests
import frappe
from frappe import enqueue
from google.oauth2 import service_account
from google.auth.transport import requests as google_requests
from frappe.utils import now, add_to_date, get_datetime, now_datetime
import re

# Connect timeout, read timeout. Without these a hung connection parks an RQ worker
# indefinitely.
FCM_TIMEOUT = (3.05, 10)


def cleanhtml(raw_html):
    cleanr = re.compile('<.*?>')
    cleantext = re.sub(cleanr, '', raw_html)
    return cleantext


def fcm_error_summary(response):
    """Summarise an FCM error response for logging.

    Never returns the response body verbatim: FCM echoes the device token back in
    INVALID_ARGUMENT responses, and Error Log is readable by every System Manager.
    """
    try:
        error = (response.json() or {}).get("error") or {}
    except ValueError:
        error = {}

    codes = [d.get("errorCode") for d in error.get("details") or [] if d.get("errorCode")]
    parts = [f"HTTP {response.status_code}", error.get("status") or "", " ".join(codes)]
    return " ".join(p for p in parts if p)


def user_id(doc):
    user_email = doc.for_user
    user_device_id = frappe.get_all(
        "User Device", filters={"user": user_email}, fields=["device_token"]
    )
    return user_device_id

def notification_queue(doc,method):
    device_token = user_id(doc)
    if device_token:
        for device in device_token:
            enqueue(
                send_fcm_notification,
                queue="default",
                now=False,
                device_token=device,
                notification=doc
            )

    # enqueue(
    #     send_fcm_notification,
    #     queue="default",
    #     now=False,
    #     notification=doc
    # )


def get_fcm_credentials():
    """
    Retrieves FCM credentials from FCM Notification Settings DocType.

    Internal only. Must never be whitelisted: it returns the decrypted service
    account private key.
    """
    credentials_doc = frappe.get_single("FCM Notification Settings")
    service_account_info = {
        "type": "service_account",
        "project_id": credentials_doc.get("project_id"),
        "private_key_id": credentials_doc.get("private_key_id"),
        "private_key": credentials_doc.get_password("private_key").replace("\\n", "\n").strip(),
        "client_email": credentials_doc.get("client_email"),
        "client_id": credentials_doc.get("client_id"),
        "auth_uri": credentials_doc.get("auth_uri"),
        "token_uri": credentials_doc.get("token_uri"),
        "auth_provider_x509_cert_url": credentials_doc.get("auth_provider_x509_cert_url"),
        "client_x509_cert_url": credentials_doc.get("client_x509_cert_url")
    }
    return service_account_info

def get_cached_access_token():
    """
    Retrieves the cached access token if valid, otherwise generates a new one.

    Internal only. Must never be whitelisted: it returns a live OAuth bearer token.
    """
    try:
        credentials_doc = frappe.get_single("FCM Notification Settings")

        # expiration_time comes back as a str from the Singles cache, and get_datetime("")
        # returns None - both compare badly against a datetime, so guard on each.
        expires_at = (
            get_datetime(credentials_doc.expiration_time) if credentials_doc.expiration_time else None
        )
        if credentials_doc.access_token and expires_at and expires_at > now_datetime():
            return {"access_token": credentials_doc.get_password("access_token")}

        service_account_info = get_fcm_credentials()
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=["https://www.googleapis.com/auth/firebase.messaging"]
        )

        request = google_requests.Request()
        credentials.refresh(request)

        access_token = credentials.token
        expiration_time = add_to_date(now(), minutes=55)

        credentials_doc.access_token = access_token
        credentials_doc.expiration_time = expiration_time
        credentials_doc.save()
        frappe.db.commit()


        return {"access_token": access_token}
    
    except Exception as e:
        frappe.log_error(title="FCM Access Token Error", message=f"Error in get_cached_access_token: {str(e)}")
        return {"error": str(e)}

def send_fcm_notification(notification,device_token): #Add device token #add doc method here when implementin notification log with device token
    """
    Sends a push notification using the cached access token.

    Internal only - invoked through frappe.enqueue, never over HTTP.
    """

    body = notification.email_content
    title = notification.subject

    body = cleanhtml(body)
    title = cleanhtml(title)

    notification_sound = frappe.db.get_single_value("FCM Notification Settings", "notification_sound") or "cash_notification.wav"

    if isinstance(device_token, dict):
        device_token = device_token.get('device_token')
    access_token = get_cached_access_token()
    headers = {
        'Authorization': f'Bearer {access_token["access_token"]}',
        'Content-Type': 'application/json; UTF-8',
    }
    payload = {
        "message": {
            "token": device_token,
            
            "notification": {
                "title": title,
                "body": body,
            },
        "android": {
            "notification": {
                "sound": notification_sound
            }
        },
        "apns": {
            "payload":{
                "aps":{
                    "sound": notification_sound,
                }
            }
        },
            "data": {
                "doctype": notification.document_type,
                "docname": str(notification.document_name),
            }
        }
    }

    fcm_endpoint = f'https://fcm.googleapis.com/v1/projects/{get_fcm_credentials()["project_id"]}/messages:send'

    try:
        response = requests.post(fcm_endpoint, headers=headers, json=payload, timeout=FCM_TIMEOUT)
    except requests.RequestException as e:
        error_message = f"Failed to send notification: {e.__class__.__name__}"
        frappe.log_error(title="FCM Notification Error", message=error_message)
        return {"status": "failed", "error": error_message}

    if response.status_code == 200:
        return {"status": "success", "response": response.json()}

    else:
        error_message = f"Failed to send notification: {fcm_error_summary(response)}"
        frappe.log_error(title="FCM Notification Error", message=error_message)
        return {"status": "failed", "error": error_message}
