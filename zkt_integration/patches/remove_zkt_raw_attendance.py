import frappe

# ZKT Raw Attendance was an intermediate copy of the device log. The sync now
# writes Employee Checkin directly, so the DocType and its table go away.
# Frappe removes the DocType record but never drops the table itself, so the
# DROP is explicit. The rows were only a cache of what the device still holds.


def execute():
	if frappe.db.exists("DocType", "ZKT Raw Attendance"):
		frappe.delete_doc("DocType", "ZKT Raw Attendance", force=True, ignore_permissions=True)
		print("Removed DocType: ZKT Raw Attendance")
	frappe.db.sql_ddl("DROP TABLE IF EXISTS `tabZKT Raw Attendance`")
