"""
ZKTeco push relay (ADMS) for an always-on office PC.

The attendance device sends its data to this program instead of to BioTime.
Every request is passed on unchanged to BioTime, and BioTime's answer goes back
to the device, so BioTime keeps working exactly as before.

A copy of each attendance upload (ATTLOG) is first saved in the "queue" folder
and then delivered to ERPNext, with retries. A slow or offline ERPNext never
affects the device or BioTime, and no punch is lost while ERPNext is away.
Delivered uploads are kept in the "archive" folder, so they can be sent again,
for example to a new ERPNext after moving from staging to production:

    python zkt_relay.py --replay-archive [--since YYYY-MM-DD]

ERPNext requests carry the device's Relay Token (erpnext_token) and, when
erpnext_site is set, the site name (X-Frappe-Site-Name) for a bench reached by IP.

Standard library only (Python 3.8+). Settings: config.json next to this file.
Status page: http://localhost:<port>/health
"""

import argparse
import base64
import datetime
import json
import logging
import logging.handlers
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULTS = {
	"listen_host": "0.0.0.0",
	"listen_port": 8000,
	"biotime_url": "",
	"erpnext_url": "",
	"erpnext_token": "",
	"erpnext_site": "",
	"serials": [],
	"forward_timeout": 30,
	"erpnext_timeout": 30,
	"retry_seconds": 60,
	"heartbeat_copy_seconds": 300,
	"queue_dir": "queue",
	"archive_dir": "archive",
	"log_file": "relay.log",
}
TOKEN_HEADER = "X-ZKT-Relay-Token"

# Headers that belong to one connection only and must not be passed on.
HOP_BY_HOP = {
	"connection",
	"keep-alive",
	"proxy-authenticate",
	"proxy-authorization",
	"te",
	"trailers",
	"transfer-encoding",
	"upgrade",
	"host",
	"content-length",
}

CFG = {}
LOG = logging.getLogger("zkt_relay")
WAKE = threading.Event()


def load_config():
	cfg = dict(DEFAULTS)
	with open(CONFIG_PATH, encoding="utf-8") as f:
		cfg.update(json.load(f))
	cfg["biotime_url"] = cfg["biotime_url"].strip().rstrip("/")
	cfg["erpnext_url"] = (cfg.get("erpnext_url") or "").strip().rstrip("/")
	cfg["erpnext_token"] = (cfg.get("erpnext_token") or "").strip()
	cfg["erpnext_site"] = (cfg.get("erpnext_site") or "").strip()
	cfg["serials"] = {s.strip().upper() for s in cfg["serials"] if s.strip()}
	for key in ("queue_dir", "archive_dir", "log_file"):
		if not os.path.isabs(cfg[key]):
			cfg[key] = os.path.join(BASE_DIR, cfg[key])
	return cfg


def setup_logging(path):
	handler = logging.handlers.RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
	handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
	LOG.addHandler(handler)
	LOG.setLevel(logging.INFO)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
	"""Hand redirects back to the device unchanged instead of following them."""

	def redirect_request(self, *args, **kwargs):
		return None


# No system proxy: BioTime is on the LAN and ERPNext is reached directly.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def send(base_url, method, path_qs, body, headers, timeout):
	"""Send one request upstream. Returns (status, content_type, body); raises if unreachable."""
	data = body if (body or method in ("POST", "PUT")) else None
	req = urllib.request.Request(base_url + path_qs, data=data, method=method)
	for key, value in headers.items():
		req.add_header(key, value)
	try:
		with OPENER.open(req, timeout=timeout) as resp:
			return resp.status, resp.headers.get("Content-Type"), resp.read()
	except urllib.error.HTTPError as e:  # an answer from upstream, just not a 2xx one
		content_type = e.headers.get("Content-Type") if e.headers else None
		return e.code, content_type, e.read() or b""


def erpnext_headers(headers):
	"""The device's headers plus what ERPNext needs: the Relay Token and, for a bench
	reached by IP, the site name. Never sent to BioTime."""
	out = dict(headers)
	if CFG["erpnext_token"]:
		out[TOKEN_HEADER] = CFG["erpnext_token"]
	if CFG["erpnext_site"]:
		out["X-Frappe-Site-Name"] = CFG["erpnext_site"]
	return out


class State:
	"""What the /health page shows."""

	def __init__(self):
		self.lock = threading.Lock()
		self.data = {
			"started": time.strftime("%Y-%m-%d %H:%M:%S"),
			"device_last_seen": None,
			"device_ip": None,
			"device_last_path": None,
			"biotime_last_status": None,
			"biotime_last_time": None,
			"biotime_last_error": None,
			"erpnext_last_ok": None,
			"erpnext_last_error": None,
			"attlog_uploads": 0,
		}

	def set(self, **kwargs):
		with self.lock:
			self.data.update(kwargs)

	def bump(self, key):
		with self.lock:
			self.data[key] += 1

	def snapshot(self):
		with self.lock:
			return dict(self.data)


STATE = State()


class Queue:
	"""Attendance uploads waiting for ERPNext, one file each, oldest first. Delivered
	ones move to the archive, in one folder per month."""

	def __init__(self, folder, archive_folder):
		self.folder = folder
		self.archive_folder = archive_folder
		os.makedirs(folder, exist_ok=True)
		os.makedirs(archive_folder, exist_ok=True)

	def put(self, path_qs, body, headers):
		name = f"{time.time():.6f}-{uuid.uuid4().hex[:8]}"
		item = {
			"saved": time.strftime("%Y-%m-%d %H:%M:%S"),
			"path_qs": path_qs,
			"headers": headers,
			"body": base64.b64encode(body).decode("ascii"),
		}
		tmp = os.path.join(self.folder, name + ".tmp")
		with open(tmp, "w", encoding="utf-8") as f:
			json.dump(item, f)
			f.flush()
			os.fsync(f.fileno())
		os.replace(tmp, os.path.join(self.folder, name + ".json"))

	def names(self):
		return sorted(n for n in os.listdir(self.folder) if n.endswith(".json"))

	def load(self, name):
		with open(os.path.join(self.folder, name), encoding="utf-8") as f:
			item = json.load(f)
		item["body"] = base64.b64decode(item["body"])
		return item

	def archive(self, name):
		month = datetime.datetime.fromtimestamp(float(name.split("-")[0])).strftime("%Y-%m")
		folder = os.path.join(self.archive_folder, month)
		os.makedirs(folder, exist_ok=True)
		os.replace(os.path.join(self.folder, name), os.path.join(folder, name))

	def replay(self, since=None):
		"""Copy archived uploads (saved on or after `since`, a date) back into the queue,
		oldest first. Ones already waiting are left alone. Returns how many were added."""
		added = 0
		for month in sorted(os.listdir(self.archive_folder)):
			folder = os.path.join(self.archive_folder, month)
			if not os.path.isdir(folder):
				continue
			for name in sorted(n for n in os.listdir(folder) if n.endswith(".json")):
				saved = datetime.datetime.fromtimestamp(float(name.split("-")[0])).date()
				if since and saved < since:
					continue
				target = os.path.join(self.folder, name)
				if os.path.exists(target):
					continue
				with open(os.path.join(folder, name), "rb") as src:
					data = src.read()
				tmp = target[: -len(".json")] + ".tmp"
				with open(tmp, "wb") as dst:
					dst.write(data)
				os.replace(tmp, target)
				added += 1
		return added


QUEUE = None


def deliver(item):
	"""Hand one queued upload to ERPNext. True only when ERPNext confirms it with "OK"."""
	try:
		status, _, data = send(
			CFG["erpnext_url"],
			"POST",
			item["path_qs"],
			item["body"],
			erpnext_headers(item["headers"]),
			CFG["erpnext_timeout"],
		)
	except Exception as e:
		return False, f"unreachable: {e}"
	text = data.decode("utf-8", "replace").strip()
	if 200 <= status < 300 and text.upper().startswith("OK"):
		return True, text
	return False, f"HTTP {status}: {text[:200]}"


def drain_forever():
	"""Deliver queued uploads in order; on the first failure wait and try again later."""
	while True:
		WAKE.wait(CFG["retry_seconds"])
		WAKE.clear()
		if not CFG["erpnext_url"]:
			continue
		try:
			for name in QUEUE.names():
				ok, detail = deliver(QUEUE.load(name))
				if not ok:
					STATE.set(erpnext_last_error=f"{time.strftime('%Y-%m-%d %H:%M:%S')} {detail}")
					LOG.warning("ERPNext not reached, %s waiting: %s", len(QUEUE.names()), detail)
					break
				QUEUE.archive(name)
				STATE.set(erpnext_last_ok=time.strftime("%Y-%m-%d %H:%M:%S"), erpnext_last_error=None)
				LOG.info("ERPNext took %s: %s", name, detail)
		except Exception:
			LOG.exception("queue delivery failed")


_last_heartbeat_copy = 0.0


def copy_heartbeat(path_qs, headers):
	"""Let ERPNext see now and then that the device is alive. Best effort, never queued.

	Sent only while nothing waits in the queue: ERPNext takes a heartbeat as "every punch
	up to now has arrived" (it moves Shift Type Last Sync of Checkin), which is only true
	when the queue is empty."""
	global _last_heartbeat_copy
	now = time.time()
	if not CFG["erpnext_url"] or now - _last_heartbeat_copy < CFG["heartbeat_copy_seconds"]:
		return
	if QUEUE.names():
		return
	_last_heartbeat_copy = now

	def run():
		try:
			send(CFG["erpnext_url"], "GET", path_qs, b"", erpnext_headers(headers), CFG["erpnext_timeout"])
		except Exception:
			pass

	threading.Thread(target=run, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.1"
	server_version = "ZKTRelay/1.0"
	timeout = 120  # drop idle keep-alive connections

	def do_GET(self):
		self.handle_any()

	def do_POST(self):
		self.handle_any()

	def do_HEAD(self):
		self.handle_any()

	def log_message(self, fmt, *args):
		pass  # our own log lines are written in handle_any

	def read_body(self):
		if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
			chunks = []
			while True:
				size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
				if size == 0:
					self.rfile.readline()
					break
				chunks.append(self.rfile.read(size))
				self.rfile.readline()
			return b"".join(chunks)
		length = int(self.headers.get("Content-Length") or 0)
		return self.rfile.read(length) if length > 0 else b""

	def reply(self, status, data=b"", content_type="text/plain"):
		self.send_response(status)
		self.send_header("Content-Type", content_type or "text/plain")
		self.send_header("Content-Length", str(len(data)))
		self.end_headers()
		if self.command != "HEAD":
			self.wfile.write(data)

	def handle_any(self):
		try:
			body = self.read_body()
			parts = urlsplit(self.path)
			path = parts.path

			if path == "/health":
				info = STATE.snapshot()
				info["queue_waiting"] = len(QUEUE.names())
				info["biotime_url"] = CFG["biotime_url"]
				info["erpnext_url"] = CFG["erpnext_url"] or "(not set - uploads only queued)"
				info["erpnext_site"] = CFG["erpnext_site"] or None
				info["erpnext_token_set"] = bool(CFG["erpnext_token"])
				return self.reply(200, json.dumps(info, indent=1).encode(), "application/json")

			if not (path == "/iclock" or path.startswith("/iclock/")):
				return self.reply(404, b"Not found")

			args = {k.lower(): v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
			serial = (args.get("sn") or "").strip().upper()
			endpoint = path[len("/iclock"):].strip("/").lower()
			path_qs = path + ("?" + parts.query if parts.query else "")
			headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
			ours = serial in CFG["serials"]
			STATE.set(
				device_last_seen=time.strftime("%Y-%m-%d %H:%M:%S"),
				device_ip=self.client_address[0],
				device_last_path=path_qs[:120],
			)

			# Save the ERPNext copy BEFORE BioTime: if anything fails after this, the
			# device sends the same punches again and ERPNext skips the ones it has.
			is_attlog = self.command == "POST" and endpoint.startswith("cdata") and (
				(args.get("table") or "").upper() == "ATTLOG"
			)
			if ours and is_attlog:
				QUEUE.put(path_qs, body, headers)
				STATE.bump("attlog_uploads")
				WAKE.set()

			try:
				status, content_type, data = send(
					CFG["biotime_url"], self.command, path_qs, body, headers, CFG["forward_timeout"]
				)
			except Exception as e:
				STATE.set(biotime_last_error=f"{time.strftime('%Y-%m-%d %H:%M:%S')} {e}")
				LOG.error("BioTime unreachable for %s %s: %s", self.command, path_qs, e)
				return self.reply(502, b"BioTime unreachable")  # the device retries later

			STATE.set(
				biotime_last_status=status,
				biotime_last_time=time.strftime("%Y-%m-%d %H:%M:%S"),
				biotime_last_error=None,
			)
			if ours and endpoint.startswith("getrequest"):
				copy_heartbeat(path_qs, headers)
			else:
				LOG.info(
					"%s %s %s (%s bytes) -> BioTime %s",
					self.client_address[0],
					self.command,
					path_qs[:160],
					len(body),
					status,
				)
			return self.reply(status, data, content_type)
		except Exception:
			LOG.exception("request failed: %s %s", self.command, self.path)
			try:
				self.reply(500, b"Relay error")
			except Exception:
				pass


def main():
	global CFG, QUEUE
	parser = argparse.ArgumentParser(description="ZKTeco push relay: device -> BioTime + ERPNext")
	parser.add_argument(
		"--replay-archive",
		action="store_true",
		help="put delivered uploads back in the queue (the running relay sends them within a minute)",
	)
	parser.add_argument("--since", help="with --replay-archive: only uploads saved on or after this date (YYYY-MM-DD)")
	options = parser.parse_args()

	CFG = load_config()
	QUEUE = Queue(CFG["queue_dir"], CFG["archive_dir"])
	if options.replay_archive:
		since = datetime.datetime.strptime(options.since, "%Y-%m-%d").date() if options.since else None
		added = QUEUE.replay(since)
		print(f"{added} archived upload(s) put back in the queue; {len(QUEUE.names())} now waiting.")
		return

	setup_logging(CFG["log_file"])
	if not CFG["biotime_url"]:
		LOG.error("biotime_url is not set in config.json: the relay needs the server the device fed before")
		return
	threading.Thread(target=drain_forever, daemon=True).start()
	WAKE.set()  # deliver anything left in the queue from before a restart
	server = ThreadingHTTPServer((CFG["listen_host"], CFG["listen_port"]), Handler)
	server.daemon_threads = True
	LOG.info(
		"relay started on %s:%s -> BioTime %s, ERPNext %s, serials %s",
		CFG["listen_host"],
		CFG["listen_port"],
		CFG["biotime_url"],
		CFG["erpnext_url"] or "(not set)",
		sorted(CFG["serials"]),
	)
	server.serve_forever()


if __name__ == "__main__":
	main()
