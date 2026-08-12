# Copyright (c) 2026, Upscape Technologies and contributors
# For license information, please see license.txt

"""Firebase Cloud Messaging transport.

Nothing in this package may be whitelisted, and nothing in it may write to the
database or commit. The only whitelisted surface in this app is `api/v1.py`.
"""

# Connect timeout, read timeout. Without these a hung connection parks an RQ worker
# indefinitely.
FCM_TIMEOUT = (3.05, 10)
