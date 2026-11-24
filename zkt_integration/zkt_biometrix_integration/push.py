"""Receiver for ZKTeco devices in PUSH (ADMS) mode.

Newer ZKTeco terminals (for example the SenseFace 2A on ZAM70 firmware) refuse the
port-4370 pull SDK that pyzk speaks. In PUSH mode the device calls this server itself,
over plain HTTP, on the paths below (all handled here):

    GET  /iclock/cdata?SN=...&options=all     handshake: the server answers with options
    POST /iclock/cdata?SN=...&table=ATTLOG    punches, one tab-separated line each
    POST /iclock/cdata?SN=...&table=<other>   operation log, photos ...: acknowledged, not used
    GET  /iclock/getrequest?SN=...            heartbeat: "OK", or a queued command
    POST /iclock/devicecmd?SN=...             the device's answer to a command

Only devices listed in ZKT Settings with Connection "Push" and their Serial Number are
answered. Any other device gets 403, so it keeps its punches and sends them once it is
added. Punches go through the same pipeline as a pull (AttendanceSyncService), so
company matching, IN/OUT and duplicate handling are identical for both kinds of device.

VIA OFFICE RELAY ("Via Office Relay" ticked on the device): a device that must keep
feeding a server on the office network, such as BioTime, is pointed at the relay program
(zkt_relay) on an always-on office PC instead. The relay passes every request to BioTime
unchanged, so BioTime keeps answering and owning the device, and delivers a copy of each
punch upload here. Such requests must carry the device's Relay Token in the
X-ZKT-Relay-Token header; without it they are refused, because this site may be reachable
from the internet. Heartbeats arriving through the relay are only noted (BioTime, not this
site, answers the device, so no command is handed out), and the ATTLOG stamp is left to
BioTime.
"""

import datetime
import hmac
import logging

import frappe
from frappe.utils import cint, get_datetime
from frappe.website.page_renderers.base_renderer import BaseRenderer
from werkzeug.wrappers import Response

from zkt_integration.zkt_biometrix_integration.script import AttendanceSyncService

PUSH = "Push"
SEEN_KEY = "zkt_push_seen"  # cache hash: serial number -> last contact
COMMANDS_KEY = "zkt_push_commands"  # cache hash: serial number -> queued commands
UNKNOWN_KEY = "zkt_push_unknown"  # cache hash: serial number -> when it was last written to the log
BAD_TOKEN_KEY = "zkt_push_bad_token"  # cache hash: serial number -> when it was last written to the log
UNKNOWN_LOG_EVERY_SECONDS = 3600
LOCK_WAIT_SECONDS = 20  # a device posting while its previous batch is still being written waits this long
ONLINE_WITHIN_SECONDS = 15 * 60  # a push device heard from this recently counts as connected
RELAY_TOKEN_HEADER = "X-ZKT-Relay-Token"


def _now():
	"""Server time, like the rest of this app (Last Run, Attendance Device Log, Shift Type sync)."""
	return datetime.datetime.now()


class ICLockPage(BaseRenderer):
	"""hooks.page_renderer: takes every request under /iclock/."""

	def can_render(self):
		return self.path.lower() == "iclock" or self.path.lower().startswith("iclock/")

	def render(self):
		return handle_request(frappe.local.request)


def handle_request(request):
	endpoint = request.path.strip("/").lower().partition("/")[2]
	args = {key.lower(): value for key, value in request.args.items()}
	serial = (args.get("sn") or "").strip()
	body = request.get_data(as_text=True) or ""
	_trace(request, body)

	if not serial:
		return _reply("Missing SN", 400)

	device = find_push_device(serial)
	if not device:
		_note_unknown_device(serial, request)
		return _reply("Unknown device", 403)

	via_relay = bool(device.get("push_via_relay"))
	if via_relay and not _relay_token_ok(device, request):
		_note_bad_token(device, request)
		return _reply("Forbidden", 403)

	# Last contact feeds Shift Type "Last Sync of Checkin". The relay sends a heartbeat only
	# when it has nothing left to deliver, so for a relay device only a heartbeat may move it:
	# an old upload arriving after an outage must not claim that everything up to now is in.
	if not via_relay or endpoint == "getrequest":
		_mark_seen(device, request, args)

	if endpoint == "cdata":
		if request.method == "GET":
			return _reply(handshake_options(device))
		if (args.get("table") or "").upper() == "ATTLOG":
			# behind the relay BioTime keeps the ATTLOG stamp; this site does not touch it
			return _receive_attlog(device, args, body, update_stamp=not via_relay)
		return _reply("OK")

	if endpoint == "getrequest":
		# behind the relay BioTime answers the device, so a command handed out here would be lost
		command = None if via_relay else _next_command(serial)
		return _reply(command or "OK")

	if endpoint == "devicecmd":
		_note_command_result(device, body)
		return _reply("OK")

	return _reply("OK")


# ----------------------------------------------------------------------
# OFFICE RELAY (the device keeps feeding BioTime; the relay delivers a copy here)
# ----------------------------------------------------------------------
def _expected_relay_token(device):
	return device.get_password("push_relay_token", raise_exception=False) or ""


def _relay_token_ok(device, request):
	"""True when the request carries the device's Relay Token (constant-time compare)."""
	expected = _expected_relay_token(device)
	given = request.headers.get(RELAY_TOKEN_HEADER) or ""
	return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


def _note_bad_token(device, request):
	"""Write a refused relay request to Attendance Device Log, at most once an hour."""
	now = _now()
	last = frappe.cache.hget(BAD_TOKEN_KEY, device.serial_number)
	if last and (now - get_datetime(last)).total_seconds() < UNKNOWN_LOG_EVERY_SECONDS:
		return
	frappe.cache.hset(BAD_TOKEN_KEY, device.serial_number, now.strftime("%Y-%m-%d %H:%M:%S"))
	_write_log(
		f"[PUSH WARNING] {device.device_id}: a request from {request.remote_addr} was refused because it "
		f"did not carry the right Relay Token. If this is the office relay, make erpnext_token in its "
		"config.json equal to the Relay Token in ZKT Settings. The relay keeps the punches and sends them again."
	)


# ----------------------------------------------------------------------
# PROTOCOL
# ----------------------------------------------------------------------
def handshake_options(device):
	"""Options sent back to the device's first request after it starts or reconnects.

	ATTLOGStamp tells the device which punches the server already has; "None" makes it
	send every punch it holds (the first contact, so nothing on the device is missed).
	Only punches are requested (TransFlag), sent as soon as they happen (Realtime).
	No TimeZone is sent: the device keeps the clock and time zone set on it.
	"""
	return "\n".join([
		f"GET OPTION FROM: {device.serial_number}",
		f"ATTLOGStamp={device.push_stamp or 'None'}",
		"OPERLOGStamp=9999",
		"ATTPHOTOStamp=9999",
		"ErrorDelay=30",
		"Delay=30",
		"TransTimes=00:00;14:05",
		"TransInterval=1",
		"TransFlag=TransData AttLog",
		"Realtime=1",
		"Encrypt=None",
		"ServerVer=2.4.1",
		"PushProtVer=2.4.1",
	])


def parse_attlog(body):
	"""ATTLOG body -> (punches, number of unreadable lines).

	Each line is "<user id>\\t<YYYY-MM-DD HH:MM:SS>\\t<state>\\t<verify mode>\\t..."; the
	punches come out as the dicts the pull pipeline reads (user_id, timestamp, punch).
	A missing state is 255, the code a device sends when it records none.
	"""
	punches, unreadable = [], 0
	for line in body.splitlines():
		if not line.strip():
			continue
		parts = [part.strip() for part in line.split("\t")]
		try:
			user_id = parts[0]
			timestamp = datetime.datetime.strptime(parts[1], "%Y-%m-%d %H:%M:%S")
		except (IndexError, ValueError):
			unreadable += 1
			continue
		if not user_id:
			unreadable += 1
			continue
		state = cint(parts[2]) if len(parts) > 2 and parts[2].lstrip("-").isdigit() else 255
		punches.append({"user_id": user_id, "timestamp": timestamp, "punch": state})
	return punches, unreadable


def _receive_attlog(device, args, body, update_stamp=True):
	"""Save the punches and say "OK" only when they are saved. On any other reply the
	sender (the device, or the office relay) keeps them and sends them again."""
	count, status = _save_pushed_attlog(device, args, body, update_stamp)
	if status == "busy":
		return _reply("Busy, send again later", 503)
	if status == "error":
		return _reply("Error, send again later", 500)
	return _reply(f"OK: {count}")


def _save_pushed_attlog(device, args, body, update_stamp):
	"""Parse an ATTLOG body and create Employee Checkins. Returns (count, status), where
	status is "ok", "busy" (another batch for this device is still being written) or
	"error"."""
	punches, unreadable = parse_attlog(body)
	count = len(punches) + unreadable
	lock = f"zkt_push_{device.name}"
	if not _acquire_lock(lock):
		return count, "busy"

	user = frappe.session.user
	try:
		# HRMS inserts the Employee Checkin with permission checks; the device has no login.
		frappe.set_user("Administrator")
		service = AttendanceSyncService(devices=[device], track_status=False)
		service.receive_pushed_logs(device, punches, unreadable)
		values = {"last_run": _now()}
		if update_stamp and args.get("stamp"):
			values["push_stamp"] = args["stamp"]
		frappe.db.set_value("ZKT Devices", device.name, values, update_modified=False)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			title=f"ZKT push: punches from {device.device_id} not saved",
			message=frappe.get_traceback() + "\n\nBody:\n" + body[:5000],
		)
		frappe.db.commit()
		return count, "error"
	finally:
		frappe.set_user(user)
		_release_lock(lock)

	return count, "ok"


# ----------------------------------------------------------------------
# DEVICES
# ----------------------------------------------------------------------
def find_push_device(serial):
	"""The ZKT Devices row of a push device by its serial number, or None."""
	serial = serial.strip().upper()
	for device in frappe.get_doc("ZKT Settings").get("devices") or []:
		if device.connection_type == PUSH and (device.serial_number or "").strip().upper() == serial:
			return device
	return None


def _mark_seen(device, request, args):
	seen = frappe.cache.hget(SEEN_KEY, device.serial_number) or {}
	seen.update({
		"time": _now().strftime("%Y-%m-%d %H:%M:%S"),
		"ip": request.remote_addr,
		"device_id": device.device_id,
	})
	if args.get("info"):
		seen["info"] = args["info"]  # firmware, user / punch counts ... sent with some heartbeats
	if args.get("pushver"):
		seen["pushver"] = args["pushver"]
	frappe.cache.hset(SEEN_KEY, device.serial_number, seen)


def last_contact(device):
	"""When a push device last called this server (datetime), or None."""
	seen = frappe.cache.hget(SEEN_KEY, (device.serial_number or "").strip()) or {}
	return get_datetime(seen["time"]) if seen.get("time") else None


def is_online(device):
	contact = last_contact(device)
	return bool(contact) and (_now() - contact).total_seconds() <= ONLINE_WITHIN_SECONDS


def _note_unknown_device(serial, request):
	"""Write an unknown device to Attendance Device Log, at most once an hour."""
	now = _now()
	last = frappe.cache.hget(UNKNOWN_KEY, serial)
	if last and (now - get_datetime(last)).total_seconds() < UNKNOWN_LOG_EVERY_SECONDS:
		return
	frappe.cache.hset(UNKNOWN_KEY, serial, now.strftime("%Y-%m-%d %H:%M:%S"))
	_write_log(
		f"[PUSH WARNING] A device with Serial Number {serial} ({request.remote_addr}) is sending data, "
		"but ZKT Settings has no Push device with that Serial Number. Its punches are refused, "
		"and it keeps them until it is added."
	)


# ----------------------------------------------------------------------
# COMMANDS (sent to the device as the answer to its heartbeat)
# ----------------------------------------------------------------------
def queue_command(serial, command):
	commands = frappe.cache.hget(COMMANDS_KEY, serial) or []
	command_id = max([c["id"] for c in commands] + [cint(frappe.cache.get_value("zkt_push_command_id"))]) + 1
	frappe.cache.set_value("zkt_push_command_id", command_id)
	commands.append({"id": command_id, "command": command})
	frappe.cache.hset(COMMANDS_KEY, serial, commands)
	return command_id


def _next_command(serial):
	commands = frappe.cache.hget(COMMANDS_KEY, serial) or []
	if not commands:
		return None
	command = commands.pop(0)
	frappe.cache.hset(COMMANDS_KEY, serial, commands)
	return f"C:{command['id']}:{command['command']}"


def _note_command_result(device, body):
	_write_log(f"Device {device.device_id} (push) answered a command: {body.strip()[:500]}")


@frappe.whitelist()
def resend_punches(device_id, from_date, to_date):
	"""Ask a push device to send its punches of a date range again, for example after
	employees got their Attendance Device ID only after they had punched. The command
	goes out with the device's next heartbeat (within a minute); punches already in
	Employee Checkin are skipped."""
	frappe.only_for("System Manager")
	from_date, to_date = get_datetime(from_date).date(), get_datetime(to_date).date()
	if from_date > to_date:
		frappe.throw(frappe._("From Date cannot be after To Date"))
	device = next(
		(d for d in frappe.get_doc("ZKT Settings").get("devices") or [] if d.device_id == device_id),
		None,
	)
	if not device or device.connection_type != PUSH or not device.serial_number:
		frappe.throw(frappe._("{0} is not a Push device with a Serial Number").format(device_id))
	if device.get("push_via_relay"):
		frappe.throw(
			frappe._(
				"{0} sends through the office relay, so BioTime, not this site, answers the device "
				"and a re-send cannot be requested from here."
			).format(device_id)
		)
	command_id = queue_command(
		device.serial_number.strip(),
		f"DATA QUERY ATTLOG StartTime={from_date} 00:00:00\tEndTime={to_date} 23:59:59",
	)
	_write_log(f"Device {device_id} (push): asked to send its punches of {from_date} to {to_date} again")
	return {"command_id": command_id, "online": is_online(device)}


@frappe.whitelist()
def get_push_status():
	"""Last contact of every push device, for the ZKT Settings form."""
	frappe.only_for("System Manager")
	rows = []
	for device in frappe.get_doc("ZKT Settings").get("devices") or []:
		if device.connection_type != PUSH:
			continue
		seen = frappe.cache.hget(SEEN_KEY, (device.serial_number or "").strip()) or {}
		rows.append({
			"device_id": device.device_id,
			"serial_number": device.serial_number,
			"last_contact": seen.get("time"),
			"ip": seen.get("ip"),
			"online": is_online(device),
			"via_relay": bool(device.get("push_via_relay")),
		})
	return rows


# ----------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------
def _reply(text, status=200):
	return Response(text, status=status, mimetype="text/plain")


def _acquire_lock(name):
	return cint(frappe.db.sql("SELECT GET_LOCK(%s, %s)", (name, LOCK_WAIT_SECONDS))[0][0]) == 1


def _release_lock(name):
	try:
		frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
	except Exception:
		pass


def _write_log(message):
	"""Attendance Device Log entry that survives a GET request (those are rolled back)."""
	frappe.get_doc({
		"doctype": "Attendance Device Log",
		"log_entry": message,
		"log_time": _now(),
	}).insert(ignore_permissions=True)
	frappe.db.commit()


def _trace(request, body):
	"""Every device request, raw, in logs/zkt_push.log: what to read when a device misbehaves."""
	try:
		logger = frappe.logger("zkt_push", allow_site=True, file_count=5, max_size=1_000_000)
		# frappe sets WARNING (ERROR in production) on every call, which would drop these lines
		logger.setLevel(logging.INFO)
		logger.info(f"{request.remote_addr} {request.method} {request.full_path} body={body[:2000]!r}")
	except Exception:
		pass
