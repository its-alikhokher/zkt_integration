# Copyright (c) 2025, ALi Raza and contributors
# For license information, please see license.txt
import frappe
from frappe.model.document import Document


class ZKTSettings(Document):
	pass


@frappe.whitelist()
def get_zkt_settings():
	return frappe.get_doc("ZKT Settings")


# The sync / clear-logs entry points live in
# zkt_integration.zkt_biometrix_integration.script and are wired up through
# hooks.scheduler_events.
