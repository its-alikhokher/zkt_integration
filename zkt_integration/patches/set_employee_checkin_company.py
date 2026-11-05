import frappe

# Employee Checkin got a Company field (fetched from the Employee). Fill it on the
# check-ins that existed before, from each one's Employee.


def execute():
	# migrate syncs custom/ folders only after the patches, so sync the field here first
	from frappe.modules.utils import sync_customizations

	sync_customizations("zkt_integration")
	if not frappe.db.has_column("Employee Checkin", "company"):
		return
	frappe.db.sql(
		"""
		UPDATE `tabEmployee Checkin` ec
		JOIN `tabEmployee` e ON e.name = ec.employee
		SET ec.company = e.company
		WHERE IFNULL(ec.company, '') = ''
		"""
	)
