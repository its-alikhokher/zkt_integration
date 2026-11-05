import frappe

# ZKT Settings got an "Enable Auto Fetch" switch. Sites that already sync keep
# syncing: a Single has no stored value for a new field, which reads as off.


def execute():
	frappe.db.set_single_value("ZKT Settings", "enable_auto_fetch", 1)
