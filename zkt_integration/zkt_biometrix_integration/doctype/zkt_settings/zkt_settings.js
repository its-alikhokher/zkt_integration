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

		zkt_show_push_status(frm);
		if (zkt_resend_devices(frm).length) {
			frm.add_custom_button(__("Re-send Punches (Push)"), () => zkt_resend_dialog(frm));
		}
	},
});

const ZKT_PUSH_API = "zkt_integration.zkt_biometrix_integration.push";

function zkt_push_devices(frm) {
	return (frm.doc.devices || []).filter((d) => d.connection_type === "Push" && d.serial_number);
}

// Behind the office relay BioTime answers the device, so only direct push devices can be
// asked to send their punches again.
function zkt_resend_devices(frm) {
	return zkt_push_devices(frm).filter((d) => !d.push_via_relay);
}

// Push devices call this site by themselves: show when each one last did, and where to point it.
function zkt_show_push_status(frm) {
	if (!zkt_push_devices(frm).length) {
		frm.set_intro("");
		return;
	}
	frappe.call({
		method: `${ZKT_PUSH_API}.get_push_status`,
		callback: (r) => {
			const esc = frappe.utils.escape_html;
			const rows = r.message || [];
			const lines = rows.map((row) => {
				const where = row.ip ? " " + __("from {0}", [esc(row.ip)]) : "";
				const state = row.online
					? __("connected, last contact {0}", [esc(row.last_contact)]) + where
					: row.last_contact
					? __("NOT connected, last contact {0}", [esc(row.last_contact)]) + where
					: __("has never called this site");
				const via = row.via_relay ? " &mdash; " + __("via the office relay") : "";
				return `<b>${esc(row.device_id)}</b> (${__("Push")}, SN ${esc(row.serial_number)}): ${state}${via}`;
			});
			if (rows.some((row) => !row.via_relay)) {
				const secure = window.location.protocol === "https:";
				const port = window.location.port || (secure ? "443" : "80");
				lines.push(
					__(
						"On a push device that sends here directly: COMM. > Cloud Server Setting > Server Address {0}, Server Port {1}, HTTPS {2}.",
						[esc(window.location.hostname), port, secure ? __("On") : __("Off")]
					)
				);
			}
			if (rows.some((row) => row.via_relay)) {
				lines.push(
					__(
						"A device via the office relay points to the relay PC, not here; the relay checks in every few minutes and delivers the punches."
					)
				);
			}
			frm.set_intro(lines.join("<br>"), rows.every((row) => row.online) ? "green" : "orange");
		},
	});
}

// Ask a push device to send a date range of punches again (e.g. after an employee got
// an Attendance Device ID only after punching). Existing check-ins are skipped.
function zkt_resend_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Re-send Punches from a Push Device"),
		fields: [
			{
				fieldname: "device_id",
				label: __("Device"),
				fieldtype: "Select",
				options: zkt_resend_devices(frm).map((dev) => dev.device_id),
				reqd: 1,
			},
			{
				fieldname: "from_date",
				label: __("From Date"),
				fieldtype: "Date",
				reqd: 1,
				default: frappe.datetime.add_days(frappe.datetime.get_today(), -7),
			},
			{
				fieldname: "to_date",
				label: __("To Date"),
				fieldtype: "Date",
				reqd: 1,
				default: frappe.datetime.get_today(),
			},
		],
		primary_action_label: __("Send Request"),
		primary_action(values) {
			frappe.call({
				method: `${ZKT_PUSH_API}.resend_punches`,
				args: values,
				callback: (r) => {
					d.hide();
					const online = r.message && r.message.online;
					frappe.msgprint(
						online
							? __("The device gets the request with its next heartbeat (within a minute). New check-ins appear in Employee Checkin after that.")
							: __("Request queued, but the device is not connected right now. It gets the request when it calls this site again.")
					);
				},
			});
		},
	});
	d.show();
}

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
