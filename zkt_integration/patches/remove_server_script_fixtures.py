import frappe

# Scheduler-event Server Scripts that this app used to ship as fixtures.
# Their jobs now run from hooks.scheduler_events, so the DB copies must go,
# otherwise every scheduled run would fire twice.
SERVER_SCRIPTS = (
	"Schedule Attendance Pull",
	"Clear Attendance Pull Logs",
)


def execute():
	for name in SERVER_SCRIPTS:
		if not frappe.db.exists("Server Script", name):
			continue
		# on_trash of Server Script also removes its linked Scheduled Job Type.
		frappe.delete_doc("Server Script", name, force=True, ignore_permissions=True)
		print(f"Removed Server Script fixture: {name}")

	frappe.cache.delete_value("server_script_map")
