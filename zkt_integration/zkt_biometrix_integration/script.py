import datetime

import frappe
from frappe.utils import cint
from frappe.utils.background_jobs import is_job_enqueued
from zk import ZK


class AttendanceSyncService:
    """Turn the attendance log of every ZKTeco device in ZKT Settings into
    Employee Checkin records.

    Per device:
      1. Collect the Attendance Device IDs of Active Employees.
      2. Pull the device log and keep only those users' punches.
      3. Create one Employee Checkin per new punch, with log_type IN or OUT.
    """

    # ZKTeco "attendance state" codes carried in each punch.
    PUNCH_IN = (0, 4)  # check-in, overtime-in
    PUNCH_OUT = (1, 5)  # check-out, overtime-out
    # A device whose punch-state option is Off sends 255 (no state). IN/OUT is
    # then derived by alternating per employee, using these two limits:
    MAX_SHIFT_HOURS = 14  # an IN older than this was never closed; next punch is a new IN
    DUPLICATE_WINDOW_SECONDS = 120  # a second punch inside this window is the same event

    def __init__(self, devices, force=False):
        self.devices = devices or []
        self.force = force  # True: ignore each device's pull frequency (manual run)

    # ------------------------------------------------------------------
    # ENTRY POINT
    # ------------------------------------------------------------------
    def run(self):
        for device in self.devices:
            last_run = device.last_run
            if last_run and not self.force:
                minutes = (datetime.datetime.now() - last_run).total_seconds() / 60
                if minutes < (device.pull_frequency or 0):
                    self._log(
                        f"Skipping device {device.device_id}; pull frequency not met "
                        f"({minutes:.0f} of {device.pull_frequency} minutes)"
                    )
                    continue

            self._log(f"Processing device: {device.device_id} (last run {last_run})")
            self._process_device(device)

            device.last_run = datetime.datetime.now()
            device.save(ignore_permissions=True)

    # ------------------------------------------------------------------
    # DEVICE PIPELINE
    # ------------------------------------------------------------------
    def _process_device(self, device):
        # Step 1: which device user IDs belong to an Active Employee?
        employees = self._active_employees_by_device_id()
        if not employees:
            self._log(f"No Active Employee has an Attendance Device ID; skipping device {device.device_id}")
            return
        self._log(
            f"{len(employees)} Active Employees have an Attendance Device ID: " + ", ".join(sorted(employees))
        )

        # Step 2: pull the device log and keep only those users' punches, oldest first.
        # The ZK protocol has no per-user filter, so the filter is applied here.
        all_logs = self._fetch_from_device(device)
        logs = sorted(
            (log for log in all_logs if str(log["user_id"]).strip() in employees),
            key=lambda log: log["timestamp"],
        )
        ignored = len(all_logs) - len(logs)
        self._log(
            f"Fetched {len(all_logs)} logs from device {device.device_id}: "
            f"{len(logs)} belong to Active Employees, {ignored} ignored (no Active Employee with that ID)"
        )
        if not logs:
            return

        # Step 3: one Employee Checkin per new punch.
        existing = self._existing_checkins(set(employees.values()), logs[0]["timestamp"])
        last = {}  # employee -> (time, log_type) of the latest punch handled so far
        created = {"IN": 0, "OUT": 0, "": 0}
        already = duplicates = failed = 0
        per_user = {}

        for log in logs:
            user_id = str(log["user_id"]).strip()
            employee = employees[user_id]
            ts = log["timestamp"]
            per_user[user_id] = per_user.get(user_id, 0) + 1

            if (employee, ts) in existing:
                last[employee] = (ts, existing[(employee, ts)])
                already += 1
                continue

            prev = last.get(employee) or self._latest_checkin_before(employee, ts)
            direction = self._resolve_direction(device, log["punch"], prev, ts)
            if direction == "DUPLICATE":
                duplicates += 1
                continue

            ok, error = self._push_to_erp(device, user_id, ts, direction)
            if ok:
                last[employee] = (ts, direction)
                created[direction] += 1
            else:
                failed += 1
                self._log(f"[ERP ERROR] {user_id} @ {ts}: {error}")

        summary = (
            f"Device {device.device_id}: {len(logs)} punches for Active Employees, "
            f"{already} already in Employee Checkin, {created['IN']} IN + {created['OUT']} OUT created"
        )
        if created[""]:
            summary += f" + {created['']} without log type"
        summary += f", {duplicates} duplicate punches ignored, {failed} rejected by HRMS"
        self._log(summary)
        self._log("Per user: " + ", ".join(f"{uid} ({count})" for uid, count in sorted(per_user.items())))

        # Only clear the device when every record on it has been handled. Punches of
        # users without an Active Employee are not stored anywhere, so clearing the
        # device while such records exist would lose them.
        if failed == 0 and ignored == 0 and device.clear_from_device_on_fetch:
            self._log("All logs synced — clearing device: " + device.device_id)
            # self._clear_device(device)
        else:
            self._log("Not clearing device — pending unsynced logs remain")

    # ------------------------------------------------------------------
    # IN / OUT
    # ------------------------------------------------------------------
    def _resolve_direction(self, device, punch, prev, ts):
        """Return "IN", "OUT", "" (leave log type empty) or "DUPLICATE" (skip punch)."""
        mode = device.punch_direction or "AUTO"
        if mode in ("IN", "OUT"):
            return mode  # device dedicated to one direction (e.g. separate entry / exit units)
        if mode == "None":
            return ""  # let the Shift Type's check-in/out rule decide

        # AUTO: trust the state the device recorded, if any.
        if punch in self.PUNCH_IN:
            return "IN"
        if punch in self.PUNCH_OUT:
            return "OUT"

        # No state from the device: alternate per employee.
        if not prev:
            return "IN"
        prev_time, prev_type = prev
        gap = (ts - prev_time).total_seconds()
        if gap <= self.DUPLICATE_WINDOW_SECONDS:
            return "DUPLICATE"
        if prev_type != "IN" or gap > self.MAX_SHIFT_HOURS * 3600:
            return "IN"
        return "OUT"

    # ------------------------------------------------------------------
    # ERPNEXT LOOKUPS / WRITES
    # ------------------------------------------------------------------
    def _active_employees_by_device_id(self):
        """{attendance_device_id: employee name} for Active Employees."""
        rows = frappe.get_all(
            "Employee",
            filters={"status": "Active", "attendance_device_id": ["is", "set"]},
            fields=["name", "attendance_device_id"],
        )
        return {str(r.attendance_device_id).strip(): r.name for r in rows if r.attendance_device_id}

    def _existing_checkins(self, employees, since):
        """{(employee, time): log_type} for check-ins from `since` onwards."""
        rows = frappe.get_all(
            "Employee Checkin",
            filters={"employee": ["in", sorted(employees)], "time": [">=", since]},
            fields=["employee", "time", "log_type"],
        )
        return {(r.employee, r.time): (r.log_type or "") for r in rows}

    def _latest_checkin_before(self, employee, ts):
        row = frappe.db.get_value(
            "Employee Checkin",
            {"employee": employee, "time": ["<", ts]},
            ["time", "log_type"],
            order_by="time desc",
            as_dict=True,
        )
        return (row.time, row.log_type or "") if row else None

    def _push_to_erp(self, device, user_id, ts, direction):
        from hrms.hr.doctype.employee_checkin.employee_checkin import (
            add_log_based_on_employee_field,
        )

        try:
            add_log_based_on_employee_field(
                employee_field_value=user_id,
                timestamp=str(ts),
                device_id=device.device_id,
                log_type=direction or None,
                latitude=device.latitude,
                longitude=device.longitude,
            )
            return True, None
        except Exception as e:
            return False, str(e)

    # ------------------------------------------------------------------
    # DEVICE ACCESS
    # ------------------------------------------------------------------
    def _comm_key(self, device):
        """Communication password of the device, as the int pyzk expects.

        Most devices ship without one, so an empty field means 0.
        """
        key = device.get_password("device_password", raise_exception=False)
        if not key:
            return 0
        try:
            return int(key)
        except (TypeError, ValueError):
            self._log(f"[DEVICE WARNING] {device.device_id} → device password must be numeric, using 0")
            return 0

    def _fetch_from_device(self, device):
        zk = ZK(device.ip, port=4370, password=self._comm_key(device))
        conn = None

        try:
            conn = zk.connect()
            conn.disable_device()
            logs = conn.get_attendance()
            return [log.__dict__ for log in logs]

        except Exception as e:
            self._log(f"[DEVICE ERROR] {device.device_id} → {str(e)}")
            return []

        finally:
            if conn:
                conn.enable_device()
                conn.disconnect()

    def _clear_device(self, device):
        self._log("Clear Device Function Currently *Commented")

    #     self._log("Clearing device:" + device.device_id)
    #     try:
    #         zk = ZK(device.ip, port=4370, password=self._comm_key(device))
    #         conn = zk.connect()
    #         conn.clear_attendance()
    #         conn.disconnect()
    #     except Exception as e:
    #         self._log(f"[CLEAR FAIL] {device.device_id} → {str(e)}")

    # ------------------------------------------------------------------
    # LOGGING
    # ------------------------------------------------------------------
    def _log(self, msg):
        print(msg)
        frappe.get_doc({
            "doctype": "Attendance Device Log",
            "log_entry": msg,
            "log_time": datetime.datetime.now(),
        }).insert(ignore_permissions=True)


# ----------------------------------------------------------------------
# PUBLIC ENTRY POINTS (Scheduler / Button / bench execute)
# ----------------------------------------------------------------------
@frappe.whitelist()
def clear_logs():
    frappe.db.delete("Attendance Device Log")
    frappe.db.commit()


SYNC_JOB_ID = "zkt_integration::sync_attendance"


@frappe.whitelist()
def sync_attendance_log_to_erpnext(force=False):
    """Pull every device in ZKT Settings into Employee Checkin.

    Scheduler (hourly_long) calls it without arguments, so each device's pull
    frequency is respected. force=True runs every device right now.
    """
    settings = frappe.get_doc("ZKT Settings")
    AttendanceSyncService(devices=settings.get("devices") or [], force=cint(force)).run()


@frappe.whitelist()
def enqueue_sync():
    """"Sync Attendance Now" button on ZKT Settings.

    Runs sync_attendance_log_to_erpnext(force=True) on the long queue, the same
    queue the scheduler uses, so a large first run cannot hit the web request
    timeout. Only one sync job is kept in the queue at a time.
    """
    frappe.only_for("System Manager")
    if is_job_enqueued(SYNC_JOB_ID):
        return {"queued": False}

    frappe.enqueue(
        "zkt_integration.zkt_biometrix_integration.script.sync_attendance_log_to_erpnext",
        queue="long",
        timeout=1500,
        job_id=SYNC_JOB_ID,
        deduplicate=True,
        force=True,
    )
    return {"queued": True}


@frappe.whitelist()
def clear_device_logs(device_id):
    settings = frappe.get_doc("ZKT Settings")
    device = None
    for d in settings.get("devices") or []:
        if d.device_id == device_id:
            device = d
            break
    if not device:
        frappe.throw(f"Device not found: {device_id}")

    AttendanceSyncService(devices=[device])._clear_device(device)
