// Copyright (c) 2025, ALi Raza and contributors
// For license information, please see license.txt

frappe.ui.form.on("ZKT Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Sync Attendance Now"), () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save ZKT Settings first, then sync."));
				return;
			}
			frappe.call({
				method: "zkt_integration.zkt_biometrix_integration.script.enqueue_sync",
				freeze: true,
				freeze_message: __("Starting attendance sync..."),
				callback(r) {
					const log_link = `<a href="/app/attendance-device-log">${__("Attendance Device Log")}</a>`;
					if (r.message && r.message.queued) {
						frappe.msgprint({
							title: __("Sync started"),
							indicator: "green",
							message: __(
								"Attendance is being pulled from all devices in the background. Progress and results: {0}",
								[log_link]
							),
						});
					} else {
						frappe.msgprint({
							title: __("Sync already running"),
							indicator: "orange",
							message: __("A sync is already queued or running. See {0}", [log_link]),
						});
					}
				},
			});
		}).addClass("btn-primary");

		frm.add_custom_button(__("Attendance Device Log"), () => {
			frappe.set_route("List", "Attendance Device Log");
		});
	},
});
