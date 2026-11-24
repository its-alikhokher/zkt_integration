import datetime
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from zkt_integration.zkt_biometrix_integration import push
from zkt_integration.zkt_biometrix_integration.script import AttendanceSyncService


def _device(**values):
	row = {
		"name": "row1",
		"device_id": "PK-Office",
		"connection_type": "Push",
		"serial_number": "ABC123",
		"company": "Test Co",
		"punch_direction": "AUTO",
		"latitude": 0,
		"longitude": 0,
		"push_stamp": None,
	}
	row.update(values)
	return frappe._dict(row)


def _request(method, path, query, body="", headers=None):
	request = MagicMock()
	request.method = method
	request.path = path
	request.args = query
	request.get_data.side_effect = lambda as_text=False: body if as_text else body.encode()
	request.remote_addr = "10.0.0.62"
	request.full_path = path
	request.query_string = "&".join(f"{k}={v}" for k, v in query.items()).encode()
	request.headers = {"Content-Type": "text/plain", "User-Agent": "iClock", **(headers or {})}
	return request


TOKEN = "relay-token-0123456789"


class TestParseAttlog(UnitTestCase):
	def test_lines_become_punches(self):
		body = "1001\t2026-11-24 09:01:02\t0\t15\t0\t0\t0\r\n1002\t2026-11-24 18:00:00\t1\t1\n\n"
		punches, unreadable = push.parse_attlog(body)
		self.assertEqual(unreadable, 0)
		self.assertEqual(
			punches,
			[
				{"user_id": "1001", "timestamp": datetime.datetime(2026, 11, 24, 9, 1, 2), "punch": 0},
				{"user_id": "1002", "timestamp": datetime.datetime(2026, 11, 24, 18, 0, 0), "punch": 1},
			],
		)

	def test_missing_state_is_255(self):
		punches, _ = push.parse_attlog("7\t2026-11-24 09:00:00")
		self.assertEqual(punches[0]["punch"], 255)
		punches, _ = push.parse_attlog("7\t2026-11-24 09:00:00\t\t15")
		self.assertEqual(punches[0]["punch"], 255)

	def test_unreadable_lines_are_counted_not_raised(self):
		punches, unreadable = push.parse_attlog("garbage\n\t2026-11-24 09:00:00\t0\n8\tnot a date\t0\n9\t2026-11-24 10:00:00\t4")
		self.assertEqual(unreadable, 3)
		self.assertEqual([p["user_id"] for p in punches], ["9"])


class TestHandshake(UnitTestCase):
	def test_first_contact_asks_for_every_punch(self):
		options = push.handshake_options(_device())
		self.assertTrue(options.startswith("GET OPTION FROM: ABC123\n"))
		self.assertIn("ATTLOGStamp=None", options)
		self.assertIn("TransFlag=TransData AttLog", options)
		self.assertIn("Realtime=1", options)
		self.assertNotIn("TimeZone", options)  # the device keeps its own clock

	def test_later_contact_sends_the_stored_stamp(self):
		self.assertIn("ATTLOGStamp=812345", push.handshake_options(_device(push_stamp="812345")))


class TestTrace(UnitTestCase):
	def test_raw_request_is_written_at_info_level(self):
		"""frappe's logger level would drop INFO lines; the trace raises it first."""
		import logging

		logger = MagicMock()
		with patch.object(push.frappe, "logger", return_value=logger):
			push._trace(_request("GET", "/iclock/getrequest", {"SN": "ABC123"}), "")
		logger.setLevel.assert_called_with(logging.INFO)
		self.assertIn("/iclock/getrequest", logger.info.call_args.args[0])


class TestRouting(UnitTestCase):
	def test_only_iclock_paths(self):
		self.assertTrue(push.ICLockPage("iclock/cdata").can_render())
		self.assertTrue(push.ICLockPage("/iclock/getrequest").can_render())
		self.assertTrue(push.ICLockPage("iClock/cdata").can_render())
		self.assertFalse(push.ICLockPage("iclocks").can_render())
		self.assertFalse(push.ICLockPage("app/zkt-settings").can_render())


class TestHandleRequest(UnitTestCase):
	def setUp(self):
		patcher = patch.object(push, "_trace")
		patcher.start()
		self.addCleanup(patcher.stop)
		patcher = patch.object(push, "_mark_seen")
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_missing_serial(self):
		response = push.handle_request(_request("GET", "/iclock/cdata", {"options": "all"}))
		self.assertEqual(response.status_code, 400)

	def test_unknown_device_is_refused_so_it_keeps_its_punches(self):
		with (
			patch.object(push, "find_push_device", return_value=None),
			patch.object(push, "_note_unknown_device") as note,
		):
			response = push.handle_request(
				_request("POST", "/iclock/cdata", {"SN": "XYZ", "table": "ATTLOG"}, "1\t2026-11-24 09:00:00\t0")
			)
		self.assertEqual(response.status_code, 403)
		note.assert_called_once()

	def test_handshake(self):
		with patch.object(push, "find_push_device", return_value=_device()):
			response = push.handle_request(_request("GET", "/iclock/cdata", {"SN": "ABC123", "options": "all"}))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.mimetype, "text/plain")
		self.assertIn("GET OPTION FROM: ABC123", response.get_data(as_text=True))

	def test_heartbeat_without_and_with_a_command(self):
		with (
			patch.object(push, "find_push_device", return_value=_device()),
			patch.object(push, "_next_command", side_effect=[None, "C:7:DATA QUERY ATTLOG x"]),
		):
			first = push.handle_request(_request("GET", "/iclock/getrequest", {"SN": "ABC123"}))
			second = push.handle_request(_request("GET", "/iclock/getrequest", {"SN": "ABC123"}))
		self.assertEqual(first.get_data(as_text=True), "OK")
		self.assertEqual(second.get_data(as_text=True), "C:7:DATA QUERY ATTLOG x")

	def test_other_tables_are_acknowledged(self):
		with (
			patch.object(push, "find_push_device", return_value=_device()),
			patch.object(push, "_receive_attlog") as receive,
		):
			response = push.handle_request(
				_request("POST", "/iclock/cdata", {"SN": "ABC123", "table": "OPERLOG"}, "OPLOG 4\t0\t...")
			)
		self.assertEqual(response.get_data(as_text=True), "OK")
		receive.assert_not_called()

	def test_attlog_goes_to_the_pipeline_and_is_acknowledged(self):
		device = _device()
		body = "1001\t2026-11-24 09:01:02\t0\t15\n1002\t2026-11-24 09:05:00\t255\t15"
		with (
			patch.object(push, "find_push_device", return_value=device),
			patch.object(push, "_acquire_lock", return_value=True),
			patch.object(push, "_release_lock"),
			patch.object(push.frappe, "set_user"),
			patch.object(push.frappe.db, "set_value") as set_value,
			patch.object(push.frappe.db, "commit"),
			patch.object(AttendanceSyncService, "receive_pushed_logs") as receive,
		):
			response = push.handle_request(
				_request("POST", "/iclock/cdata", {"SN": "ABC123", "table": "ATTLOG", "Stamp": "9001"}, body)
			)
		self.assertEqual(response.get_data(as_text=True), "OK: 2")
		punches = receive.call_args.args[1]
		self.assertEqual([p["user_id"] for p in punches], ["1001", "1002"])
		self.assertEqual(set_value.call_args.args[2]["push_stamp"], "9001")

	def test_attlog_failure_is_not_acknowledged(self):
		with (
			patch.object(push, "find_push_device", return_value=_device()),
			patch.object(push, "_acquire_lock", return_value=True),
			patch.object(push, "_release_lock"),
			patch.object(push.frappe, "set_user"),
			patch.object(push.frappe.db, "rollback"),
			patch.object(push.frappe.db, "commit"),
			patch.object(push.frappe, "log_error"),
			patch.object(AttendanceSyncService, "receive_pushed_logs", side_effect=Exception("boom")),
		):
			response = push.handle_request(
				_request("POST", "/iclock/cdata", {"SN": "ABC123", "table": "ATTLOG"}, "1\t2026-11-24 09:00:00\t0")
			)
		self.assertEqual(response.status_code, 500)  # the device keeps the punches and retries

	def test_busy_device_is_asked_to_retry(self):
		with (
			patch.object(push, "find_push_device", return_value=_device()),
			patch.object(push, "_acquire_lock", return_value=False),
		):
			response = push.handle_request(
				_request("POST", "/iclock/cdata", {"SN": "ABC123", "table": "ATTLOG"}, "1\t2026-11-24 09:00:00\t0")
			)
		self.assertEqual(response.status_code, 503)


class TestOfficeRelay(UnitTestCase):
	"""A device behind the office relay keeps feeding BioTime; the relay delivers its
	punches here and must prove itself with the device's Relay Token."""

	def setUp(self):
		patcher = patch.object(push, "_trace")
		patcher.start()
		self.addCleanup(patcher.stop)
		patcher = patch.object(push, "_mark_seen")
		self.mark_seen = patcher.start()
		self.addCleanup(patcher.stop)
		patcher = patch.object(push, "_expected_relay_token", return_value=TOKEN)
		patcher.start()
		self.addCleanup(patcher.stop)

	def _relay_request(self, method, path, query, body="", token=TOKEN):
		headers = {push.RELAY_TOKEN_HEADER: token} if token is not None else {}
		return _request(method, path, query, body, headers)

	def test_request_without_or_with_a_wrong_token_is_refused(self):
		for token in (None, "", "wrong-token-0123456789"):
			with (
				patch.object(push, "find_push_device", return_value=_device(push_via_relay=1)),
				patch.object(push, "_note_bad_token") as note,
				patch.object(push, "_receive_attlog") as receive,
			):
				response = push.handle_request(
					self._relay_request(
						"POST", "/iclock/cdata", {"SN": "ABC123", "table": "ATTLOG"}, "1\t2026-11-24 09:00:00\t0", token
					)
				)
			self.assertEqual(response.status_code, 403, token)  # the relay keeps the punches and retries
			note.assert_called_once()
			receive.assert_not_called()

	def test_device_with_no_token_set_refuses_everything(self):
		with (
			patch.object(push, "_expected_relay_token", return_value=""),
			patch.object(push, "find_push_device", return_value=_device(push_via_relay=1)),
			patch.object(push, "_note_bad_token"),
		):
			response = push.handle_request(self._relay_request("GET", "/iclock/getrequest", {"SN": "ABC123"}, token=""))
		self.assertEqual(response.status_code, 403)

	def test_attlog_with_the_token_is_recorded_and_the_stamp_left_to_biotime(self):
		body = "1001\t2026-11-24 09:01:02\t0\t15"
		with (
			patch.object(push, "find_push_device", return_value=_device(push_via_relay=1)),
			patch.object(push, "_acquire_lock", return_value=True),
			patch.object(push, "_release_lock"),
			patch.object(push.frappe, "set_user"),
			patch.object(push.frappe.db, "set_value") as set_value,
			patch.object(push.frappe.db, "commit"),
			patch.object(AttendanceSyncService, "receive_pushed_logs") as receive,
		):
			response = push.handle_request(
				self._relay_request("POST", "/iclock/cdata", {"SN": "ABC123", "table": "ATTLOG", "Stamp": "9"}, body)
			)
		self.assertEqual(response.get_data(as_text=True), "OK: 1")
		self.assertEqual([p["user_id"] for p in receive.call_args.args[1]], ["1001"])
		self.assertNotIn("push_stamp", set_value.call_args.args[2])

	def test_failed_save_is_not_acknowledged_so_the_relay_retries(self):
		with (
			patch.object(push, "find_push_device", return_value=_device(push_via_relay=1)),
			patch.object(push, "_save_pushed_attlog", return_value=(1, "error")),
		):
			response = push.handle_request(
				self._relay_request("POST", "/iclock/cdata", {"SN": "ABC123", "table": "ATTLOG"}, "1\t2026-11-24 09:00:00\t0")
			)
		self.assertEqual(response.status_code, 500)

	def test_only_a_heartbeat_counts_as_contact(self):
		"""An upload delivered late (after an outage) must not move Last Sync of Checkin;
		the relay sends a heartbeat only once its queue is empty."""
		with (
			patch.object(push, "find_push_device", return_value=_device(push_via_relay=1)),
			patch.object(push, "_receive_attlog", return_value=push._reply("OK: 1")),
		):
			push.handle_request(
				self._relay_request("POST", "/iclock/cdata", {"SN": "ABC123", "table": "ATTLOG"}, "1\t2026-11-24 09:00:00\t0")
			)
			self.mark_seen.assert_not_called()
			push.handle_request(self._relay_request("GET", "/iclock/getrequest", {"SN": "ABC123"}))
			self.mark_seen.assert_called_once()

	def test_heartbeat_hands_out_no_command(self):
		with (
			patch.object(push, "find_push_device", return_value=_device(push_via_relay=1)),
			patch.object(push, "_next_command") as next_command,
		):
			response = push.handle_request(self._relay_request("GET", "/iclock/getrequest", {"SN": "ABC123"}))
		self.assertEqual(response.get_data(as_text=True), "OK")
		next_command.assert_not_called()  # a queued command stays queued instead of being lost

	def test_direct_push_device_needs_no_token(self):
		with patch.object(push, "find_push_device", return_value=_device()):
			response = push.handle_request(_request("GET", "/iclock/cdata", {"SN": "ABC123", "options": "all"}))
		self.assertEqual(response.status_code, 200)

	def test_relay_device_cannot_use_the_resend_button(self):
		with patch("frappe.get_doc") as get_doc:
			get_doc.return_value.get.return_value = [_device(push_via_relay=1)]
			with self.assertRaises(frappe.ValidationError):
				push.resend_punches("PK-Office", "2026-11-01", "2026-11-02")


class TestRelayToken(UnitTestCase):
	def test_token_comes_from_the_password_field_and_must_match_exactly(self):
		device = MagicMock()
		device.get_password.return_value = TOKEN

		def check(token):
			return push._relay_token_ok(
				device, _request("GET", "/iclock/getrequest", {}, headers={push.RELAY_TOKEN_HEADER: token})
			)

		self.assertTrue(check(TOKEN))
		self.assertFalse(check(TOKEN + "x"))
		self.assertFalse(check(TOKEN.upper()))
		device.get_password.assert_called_with("push_relay_token", raise_exception=False)


class TestReceivePushedLogs(UnitTestCase):
	def test_only_active_employees_of_the_device_company_are_written(self):
		service = AttendanceSyncService(devices=[], track_status=False)
		punches = [
			{"user_id": "2", "timestamp": datetime.datetime(2026, 11, 24, 9, 5), "punch": 255},
			{"user_id": "1", "timestamp": datetime.datetime(2026, 11, 24, 9, 0), "punch": 0},
			{"user_id": "99", "timestamp": datetime.datetime(2026, 11, 24, 9, 1), "punch": 0},
		]
		with (
			patch.object(service, "_active_employees_by_device_id", return_value={"1": "EMP-1", "2": "EMP-2"}) as active,
			patch.object(service, "_create_checkins", return_value=0) as create,
			patch.object(service, "_log") as log,
		):
			service.receive_pushed_logs(_device(company="Test Co"), punches, unreadable=1)
		active.assert_called_once_with("Test Co")
		logs = create.call_args.args[1]
		self.assertEqual([p["user_id"] for p in logs], ["1", "2"])  # oldest first, unknown 99 left out
		message = log.call_args.args[0]
		self.assertIn("1 ignored", message)
		self.assertIn("99", message)
		self.assertIn("1 unreadable", message)

	def test_status_of_a_sync_run_is_left_alone(self):
		service = AttendanceSyncService(devices=[], track_status=False)
		with patch.object(push.frappe.cache, "set_value") as set_value:
			service._set_status(state="running")
		set_value.assert_not_called()


class TestPullStillWorks(UnitTestCase):
	"""The pull path after the split into _process_device / _create_checkins."""

	def test_pull_device_is_read_and_push_device_is_not(self):
		pull = _device(name="r1", device_id="US", connection_type="Pull", ip="1.2.3.4", last_run=None,
			pull_frequency=0, clear_from_device_on_fetch=0)
		pull.save = MagicMock()
		push_row = _device(name="r2", device_id="PK")
		punches = [
			{"user_id": "1", "timestamp": datetime.datetime(2026, 11, 24, 9, 0), "punch": 0},
			{"user_id": "5", "timestamp": datetime.datetime(2026, 11, 24, 9, 1), "punch": 0},
		]
		service = AttendanceSyncService(devices=[pull, push_row], force=True, track_status=False)
		with (
			patch.object(service, "_active_employees_by_device_id", return_value={"1": "EMP-1"}),
			patch.object(service, "_fetch_from_device", return_value=punches) as fetch,
			patch.object(service, "_create_checkins", return_value=0) as create,
			patch.object(service, "_note_push_device") as note_push,
			patch.object(service, "_update_shift_sync"),
			patch.object(service, "_log"),
			patch("frappe.db.commit"),
		):
			service.run()
		fetch.assert_called_once_with(pull)  # only the pull device is contacted
		self.assertEqual([p["user_id"] for p in create.call_args.args[1]], ["1"])
		note_push.assert_called_once()
		self.assertIs(note_push.call_args.args[0], push_row)
		pull.save.assert_called_once()  # Last Run of the pull device is written


class TestSettingsValidation(UnitTestCase):
	def _settings(self, *devices):
		return frappe.get_doc({"doctype": "ZKT Settings", "devices": list(devices)})

	def test_push_needs_serial_and_pull_needs_ip(self):
		with self.assertRaises(frappe.ValidationError):
			self._settings({"device_id": "a", "connection_type": "Push"}).validate_devices()
		with self.assertRaises(frappe.ValidationError):
			self._settings({"device_id": "b", "connection_type": "Pull"}).validate_devices()
		self._settings(
			{"device_id": "a", "connection_type": "Push", "serial_number": " SN1 "},
			{"device_id": "b", "connection_type": "Pull", "ip": "10.0.0.5"},
		).validate_devices()

	def test_serial_numbers_are_unique(self):
		with self.assertRaises(frappe.ValidationError):
			self._settings(
				{"device_id": "a", "connection_type": "Push", "serial_number": "SN1"},
				{"device_id": "b", "connection_type": "Push", "serial_number": "sn1"},
			).validate_devices()

	def test_empty_connection_means_pull(self):
		settings = self._settings({"device_id": "b", "ip": "10.0.0.5"})
		settings.validate_devices()
		self.assertEqual(settings.devices[0].connection_type, "Pull")

	def test_relay_device_needs_a_long_enough_token(self):
		row = {"device_id": "a", "connection_type": "Push", "serial_number": "SN1", "push_via_relay": 1}
		with self.assertRaises(frappe.ValidationError):
			self._settings(dict(row)).validate_devices()
		with self.assertRaises(frappe.ValidationError):
			self._settings(dict(row, push_relay_token="short")).validate_devices()
		self._settings(dict(row, push_relay_token=TOKEN)).validate_devices()
		self._settings(dict(row, push_relay_token="*" * 8)).validate_devices()  # a stored token comes back masked
