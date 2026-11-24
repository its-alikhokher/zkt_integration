# Copyright (c) 2025, ALi Raza and contributors
# For license information, please see license.txt
import frappe
from frappe import _
from frappe.model.document import Document


class ZKTSettings(Document):
	def validate(self):
		self.validate_devices()

	def validate_devices(self):
		"""A pull device is reached at its IP; a push device is recognised by its serial number."""
		serials = {}
		for device in self.get("devices") or []:
			device.connection_type = device.connection_type or "Pull"
			if device.connection_type == "Push":
				device.serial_number = (device.serial_number or "").strip()
				if not device.serial_number:
					frappe.throw(
						_("Row {0} ({1}): a Push device needs its Serial Number.").format(
							device.idx, device.device_id
						)
					)
				key = device.serial_number.upper()
				if key in serials:
					frappe.throw(
						_("Row {0}: Serial Number {1} is already used in row {2}.").format(
							device.idx, device.serial_number, serials[key]
						)
					)
				serials[key] = device.idx
				if device.get("push_via_relay"):
					self.validate_relay_token(device)
			elif not (device.ip or "").strip():
				frappe.throw(
					_("Row {0} ({1}): a Pull device needs its IP Address.").format(device.idx, device.device_id)
				)

	def validate_relay_token(self, device):
		"""A device behind the office relay needs a Relay Token. A stored one comes back
		masked as asterisks; only a newly typed one can be checked for length."""
		token = (device.get("push_relay_token") or "").strip()
		if not token:
			frappe.throw(
				_("Row {0} ({1}): a device Via Office Relay needs a Relay Token.").format(
					device.idx, device.device_id
				)
			)
		if set(token) != {"*"} and len(token) < 16:
			frappe.throw(_("Row {0}: the Relay Token must be at least 16 characters.").format(device.idx))
		device.push_relay_token = token


@frappe.whitelist()
def get_zkt_settings():
	return frappe.get_doc("ZKT Settings")


# The sync / clear-logs entry points live in
# zkt_integration.zkt_biometrix_integration.script and are wired up through
# hooks.scheduler_events.
