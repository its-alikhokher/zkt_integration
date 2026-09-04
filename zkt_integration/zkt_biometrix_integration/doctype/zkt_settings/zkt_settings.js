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

	if (status.state === "running") {
		frm.dashboard.show_progress(ZKT_TITLE, status.percent || 1, status.description || "");
		frm._zkt_poll = setTimeout(() => zkt_fetch_status(frm), 3000); // fallback when realtime is down
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
	// Runs right now, like `bench execute`; the request returns when the sync is done.
	frappe.call({
		method: `${ZKT_API}.sync_attendance_log_to_erpnext`,
		args: { force: 1 },
		callback() {
			frm.reload_doc(); // Last Run changed; refresh() then shows the result headline
		},
		error() {
			zkt_fetch_status(frm); // e.g. "already running": show that run's bar
		},
	});
}
