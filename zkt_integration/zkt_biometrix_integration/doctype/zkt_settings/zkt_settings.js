// Copyright (c) 2025, ALi Raza and contributors
// For license information, please see license.txt

const ZKT_EVENT = "zkt_sync_progress"; // must match AttendanceSyncService.PROGRESS_EVENT
const ZKT_TITLE = __("Syncing attendance");
const ZKT_API = "zkt_integration.zkt_biometrix_integration.script";

frappe.ui.form.on("ZKT Settings", {
	refresh(frm) {
		zkt_bind_realtime(frm);
		zkt_fetch_status(frm); // after a page refresh: re-show a running bar or the last result

		frm.add_custom_button(__("Sync Attendance Now"), () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save ZKT Settings first, then sync."));
				return;
			}
			zkt_sync_now(frm);
		}).addClass("btn-primary");

		frm.add_custom_button(__("Attendance Device Log"), () => {
			frappe.set_route("List", "Attendance Device Log");
		});

		frm.add_custom_button(__("Device Logs"), () => zkt_device_logs_dialog());
	},
});

function zkt_bind_realtime(frm) {
	if (frm._zkt_bound) return;
	frm._zkt_bound = true;
	frappe.realtime.on(ZKT_EVENT, (status) => zkt_render(frm, status));
}

function zkt_fetch_status(frm) {
	frappe.call({
		method: `${ZKT_API}.get_sync_status`,
		callback: (r) => zkt_render(frm, r.message),
	});
}

// Draw the sync state at the top of the form: a progress bar while running,
// the outcome as a headline once finished.
function zkt_render(frm, status) {
	if (!status || !status.state) return;
	clearTimeout(frm._zkt_poll);

	if (status.state === "running" || status.state === "queued") {
		frm._zkt_following = true;
		frm.dashboard.show_progress(ZKT_TITLE, status.percent || 1, status.description || "");
		frm._zkt_poll = setTimeout(() => zkt_fetch_status(frm), 3000); // fallback when realtime is down
		return;
	}

	if (frm._zkt_following) {
		// the run this form was following has ended: reload once for the new Last Run,
		// and refresh() then shows the result headline
		frm._zkt_following = false;
		frm.reload_doc();
		return;
	}

	if (frm.dashboard._progress_map && frm.dashboard._progress_map[ZKT_TITLE]) {
		frm.dashboard.hide_progress(ZKT_TITLE);
	}

	const lines = status.lines || [];
	const failed = status.state === "failed";
	const warn = lines.some((l) => l.includes("[DEVICE ERROR]") || l.includes("WARNING"));
	const title = failed ? __("Sync failed") : warn ? __("Sync finished with errors") : __("Sync complete");
	const color = failed ? "red" : warn ? "orange" : "green";
	const when = status.finished
		? " &middot; " + __("Completed at {0}, took {1} s", [status.finished, status.took_seconds])
		: "";
	const error = failed && status.error ? `<br>${frappe.utils.escape_html(status.error)}` : "";
	const body = lines.length
		? lines.map((l) => frappe.utils.escape_html(l)).join("<br>")
		: __("Nothing to do.");
	frm.dashboard.set_headline_alert(
		`<b>${title}</b>${when}${error}<br>${body}<br>` +
			`<a href="/app/attendance-device-log">${__("Open Attendance Device Log")}</a>`,
		color,
		true
	);
}

function zkt_sync_now(frm) {
	frm.dashboard.clear_headline();
	frm.dashboard.show_progress(ZKT_TITLE, 1, __("Starting..."));
	// Queues the sync in the background and returns at once; the bar then follows the job.
	frappe.call({
		method: `${ZKT_API}.sync_attendance_log_to_erpnext`,
		args: { force: 1 },
		callback() {
			zkt_fetch_status(frm);
		},
		error() {
			zkt_fetch_status(frm); // e.g. "already running": show that run's bar
		},
	});
}

// Show one employee's punches exactly as the devices hold them, read live from
// the devices (not from Employee Checkin), for a date range.
function zkt_device_logs_dialog() {
	const d = new frappe.ui.Dialog({
		title: __("Device Logs"),
		size: "large",
		fields: [
			{
				fieldname: "employee",
				label: __("Employee"),
				fieldtype: "Link",
				options: "Employee",
				reqd: 1,
				get_query: () => ({ filters: { attendance_device_id: ["is", "set"] } }),
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "from_date",
				label: __("From Date"),
				fieldtype: "Date",
				reqd: 1,
				default: frappe.datetime.add_days(frappe.datetime.get_today(), -30),
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "to_date",
				label: __("To Date"),
				fieldtype: "Date",
				reqd: 1,
				default: frappe.datetime.get_today(),
			},
			{ fieldtype: "Section Break" },
			{ fieldname: "logs", fieldtype: "HTML" },
		],
		primary_action_label: __("Fetch from Device"),
		primary_action(values) {
			frappe.call({
				method: `${ZKT_API}.get_device_logs`,
				args: values,
				freeze: true,
				freeze_message: __("Reading the devices..."),
				callback: (r) => zkt_render_device_logs(d, r.message || {}),
			});
		},
	});
	d.show();
}

function zkt_render_device_logs(d, data) {
	const esc = frappe.utils.escape_html;
	const logs = data.logs || [];
	let html = "";
	if ((data.unreachable || []).length) {
		html += `<div class="alert alert-warning">${__("Could not read: {0}", [esc(data.unreachable.join(", "))])}</div>`;
	}
	if (!logs.length) {
		html += `<p class="text-muted">${__("No punches on the devices for this employee in these dates.")}</p>`;
	} else {
		html += `<p class="text-muted">${__("{0} punches on the devices (Device User ID {1})", [logs.length, esc(data.device_user_id)])}</p>`;
		html += `<div style="max-height: 55vh; overflow: auto">
			<table class="table table-bordered table-sm">
				<thead><tr>
					<th style="width: 50px">#</th><th>${__("Time")}</th><th>${__("Device")}</th><th>${__("Punch")}</th>
				</tr></thead><tbody>`;
		logs.forEach((l, i) => {
			html += `<tr><td>${i + 1}</td><td>${esc(l.time)}</td><td>${esc(l.device_id)}</td><td>${esc(l.punch)}</td></tr>`;
		});
		html += "</tbody></table></div>";
	}
	d.get_field("logs").$wrapper.html(html);
}
