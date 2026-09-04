import json

import frappe

EXAMPLE_DEVICE_ID = "example-device"


def after_install():
	setup_default_settings()


def setup_default_settings():
	"""Create ZKT Settings with an example device row and an example shift mapping.

	The example shows the expected format; replace it with real devices.
	Shift Types are left to HRMS: one default shift is created only when the
	site has none at all.
	"""
	if frappe.db.count("Shift Type") == 0:
		create_default_shift_type()

	settings = frappe.get_doc("ZKT Settings")
	added_example = False

	if not settings.get("devices"):
		settings.append("devices", {
			"device_id": EXAMPLE_DEVICE_ID,
			"ip": "192.168.1.201",
			"punch_direction": "AUTO",
			"clear_from_device_on_fetch": 0,
			"latitude": 0,
			"longitude": 0,
			"pull_frequency": 60,
		})
		added_example = True

	mapping = (settings.shift_type_device_mapping or "").strip()
	if mapping in ("", "{}", "[]"):
		shift = frappe.get_all("Shift Type", pluck="name", order_by="creation asc", limit=1)
		if added_example and shift:
			settings.shift_type_device_mapping = json.dumps(
				[{"shift_type_name": shift[0], "related_device_id": [EXAMPLE_DEVICE_ID]}]
			)
		else:
			settings.shift_type_device_mapping = "[]"

	settings.save(ignore_permissions=True)
	print(
		"ZKT Settings ready with an example device. Replace it with your device's IP, "
		"then set Attendance Device ID on your Employees."
	)


def create_default_shift_type():
	"""Only called when the site has no Shift Type at all."""
	if frappe.db.exists("Shift Type", "Standard Office Shift"):
		return
	frappe.get_doc({
		"doctype": "Shift Type",
		"name": "Standard Office Shift",
		"start_time": "09:00:00",
		"end_time": "18:00:00",
		"determine_check_in_and_check_out": "Alternating entries as IN and OUT during the same shift",
		"working_hours_calculation_based_on": "First Check-in and Last Check-out",
		"begin_check_in_before_shift_start_time": 60,
		"allow_check_out_after_shift_end_time": 60,
		"process_attendance_after": frappe.utils.today(),
	}).insert(ignore_permissions=True)
	print("Shift Type 'Standard Office Shift' created (site had none).")
