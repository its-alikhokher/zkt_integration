// Copyright (c) 2025, ALi Raza and contributors
// For license information, please see license.txt

frappe.listview_settings["Attendance Device Log"] = {
	onload(listview) {
		const btn = listview.page.add_inner_button(__("Clear Logs"), () => {
			frappe.call({
				method: "zkt_integration.zkt_biometrix_integration.script.clear_logs",
				freeze: true,
				freeze_message: __("Clearing logs..."),
				callback(r) {
					const n = (r.message && r.message.deleted) || 0;
					frappe.show_alert({ message: __("{0} log entries cleared.", [n]), indicator: "green" });
					listview.refresh();
				},
			});
		});

		// Make button RED
		btn.removeClass("btn-default").addClass("btn-danger");
	},
};
