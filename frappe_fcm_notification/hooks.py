app_name = "frappe_fcm_notification"
app_title = "Frappe Fcm Notification"
app_publisher = "Upscape Technologies"
app_description = "Trigger fcm notification from frappe"
app_email = "info@upscapetech.com"
app_license = "mit"
# required_apps = []

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/frappe_fcm_notification/css/frappe_fcm_notification.css"
# app_include_js = "/assets/frappe_fcm_notification/js/frappe_fcm_notification.js"

# include js, css files in header of web template
# web_include_css = "/assets/frappe_fcm_notification/css/frappe_fcm_notification.css"
# web_include_js = "/assets/frappe_fcm_notification/js/frappe_fcm_notification.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "frappe_fcm_notification/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "frappe_fcm_notification/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "frappe_fcm_notification.utils.jinja_methods",
# 	"filters": "frappe_fcm_notification.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "frappe_fcm_notification.install.before_install"
# after_install = "frappe_fcm_notification.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "frappe_fcm_notification.uninstall.before_uninstall"
# after_uninstall = "frappe_fcm_notification.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "frappe_fcm_notification.utils.before_app_install"
# after_app_install = "frappe_fcm_notification.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "frappe_fcm_notification.utils.before_app_uninstall"
# after_app_uninstall = "frappe_fcm_notification.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "frappe_fcm_notification.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

doc_events = {
    "Notification Log": {
        "after_insert": "frappe_fcm_notification.engine.dispatch.on_notification_log"
    },
    "Push Device": {
        "after_insert": "frappe_fcm_notification.engine.topics.enqueue_device_sync",
        "on_update": "frappe_fcm_notification.engine.topics.enqueue_device_sync",
    },
}

scheduler_events = {
    "cron": {
        # Starts due campaigns, requeues batches waiting on a retry, and buries
        # campaigns that have been Running too long to recover.
        "*/5 * * * *": ["frappe_fcm_notification.engine.dispatch.run_scheduled_campaigns"]
    },
    "daily_long": [
        # Topic subscriptions drift: calls fail silently and roles change. The device
        # row records what we believe, this repairs what is actually true.
        "frappe_fcm_notification.engine.topics.reconcile_topics"
    ],
}

# Delivery logs are diagnostics, not records. Failures and a small success sample only,
# cleared on the standard Log Settings schedule.
default_log_clearing_doctypes = {"Push Delivery Log": 30}

# Allowlist hooks. Both take dotted paths, and both are hooks rather than doctype fields
# on purpose: a path that can only come from installed app code cannot be pointed at
# arbitrary callables by anyone with write access to a document.
#
# fcm_project_resolver: fn(doc) -> project name, for sites serving several Firebase
# projects from one bench. Falls back to the FCM Project flagged is_default.
fcm_project_resolver = []

# fcm_audience_resolvers: dotted paths permitted as Push Audience "Method" resolvers.
# Consumed from Milestone 2.
fcm_audience_resolvers = []

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"frappe_fcm_notification.tasks.all"
# 	],
# 	"daily": [
# 		"frappe_fcm_notification.tasks.daily"
# 	],
# 	"hourly": [
# 		"frappe_fcm_notification.tasks.hourly"
# 	],
# 	"weekly": [
# 		"frappe_fcm_notification.tasks.weekly"
# 	],
# 	"monthly": [
# 		"frappe_fcm_notification.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "frappe_fcm_notification.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "frappe_fcm_notification.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "frappe_fcm_notification.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["frappe_fcm_notification.utils.before_request"]
# after_request = ["frappe_fcm_notification.utils.after_request"]

# Job Events
# ----------
# before_job = ["frappe_fcm_notification.utils.before_job"]
# after_job = ["frappe_fcm_notification.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"frappe_fcm_notification.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

