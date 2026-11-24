"""
ZKTeco push relay (ADMS) for an always-on office PC.

The attendance device sends its data to this program instead of to BioTime.
Every request is passed on unchanged to BioTime, and BioTime's answer goes back
to the device, so BioTime keeps working exactly as before.

Each attendance upload (ATTLOG) is saved in the "archive" folder and in one queue
per ERPNext target, then delivered to every target with retries. Targets are
independent: a slow or offline one never delays another, the device or BioTime,
and nothing is lost while a target is away. The archive keeps every upload, so it
can be sent again, for example to a new target after moving to production:

    python zkt_relay.py --replay-archive [--since YYYY-MM-DD] [--target NAME]

Requests to a target carry its Relay Token and, when "site" is set, the site name
(X-Frappe-Site-Name) for a bench reached by IP address.

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
import re
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
	# Several ERPNext sites: [{"name": "...", "url": "...", "site": "", "token": "..."}, ...].
	# Older single-site settings (erpnext_url / erpnext_site / erpnext_token) still work.
	"erpnext_targets": None,
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
TARGETS = []
ARCHIVE = None


def now_text():
	return time.strftime("%Y-%m-%d %H:%M:%S")


def load_config():
	cfg = dict(DEFAULTS)
	with open(CONFIG_PATH, encoding="utf-8") as f:
		cfg.update(json.load(f))
	cfg["biotime_url"] = (cfg.get("biotime_url") or "").strip().rstrip("/")
	cfg["serials"] = {s.strip().upper() for s in cfg["serials"] if s.strip()}
	for key in ("queue_dir", "archive_dir", "log_file"):
		if not os.path.isabs(cfg[key]):
			cfg[key] = os.path.join(BASE_DIR, cfg[key])

	targets = cfg.get("erpnext_targets")
	if not targets and (cfg.get("erpnext_url") or "").strip():
		targets = [
			{
				"name": "erpnext",
				"url": cfg["erpnext_url"],
				"site": cfg.get("erpnext_site") or "",
				"token": cfg.get("erpnext_token") or "",
			}
		]
	cfg["targets"] = []
	for index, target in enumerate(targets or [], start=1):
		url = (target.get("url") or "").strip().rstrip("/")
		if not url:
			continue
		name = re.sub(r"[^A-Za-z0-9_.-]", "_", (target.get("name") or "").strip()) or f"target{index}"
		if name in {t["name"] for t in cfg["targets"]}:
			raise ValueError(f"two ERPNext targets are named {name!r} in config.json")
		cfg["targets"].append(
			{
				"name": name,
				"url": url,
				"site": (target.get("site") or "").strip(),
				"token": (target.get("token") or "").strip(),
			}
		)
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


def write_file(path, data):
	"""Write atomically: a reader never sees half a file."""
	tmp = path[: -len(".json")] + ".tmp" if path.endswith(".json") else path + ".tmp"
	with open(tmp, "wb") as f:
		f.write(data)
		f.flush()
		os.fsync(f.fileno())
	os.replace(tmp, path)


def upload_date(name):
	return datetime.datetime.fromtimestamp(float(name.split("-")[0])).date()


class State:
	"""What the /health page shows about the device and BioTime."""

	def __init__(self):
		self.lock = threading.Lock()
		self.data = {
			"started": now_text(),
			"device_last_seen": None,
			"device_ip": None,
			"device_last_path": None,
			"biotime_last_status": None,
			"biotime_last_time": None,
			"biotime_last_error": None,
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


class Archive:
	"""Every attendance upload as received, one folder per month, oldest first."""

	def __init__(self, folder):
		self.folder = folder
		os.makedirs(folder, exist_ok=True)

	def save(self, name, data):
		month = os.path.join(self.folder, upload_date(name).strftime("%Y-%m"))
		os.makedirs(month, exist_ok=True)
		path = os.path.join(month, name)
		if not os.path.exists(path):
			write_file(path, data)

	def items(self, since=None):
		for month in sorted(os.listdir(self.folder)):
			folder = os.path.join(self.folder, month)
			if not os.path.isdir(folder):
				continue
			for name in sorted(n for n in os.listdir(folder) if n.endswith(".json")):
				if since and upload_date(name) < since:
					continue
				yield name, os.path.join(folder, name)


class Target:
	"""One ERPNext site: its own queue folder, delivery thread and status."""

	def __init__(self, conf, queue_root):
		self.name = conf["name"]
		self.url = conf["url"]
		self.site = conf["site"]
		self.token = conf["token"]
		self.folder = os.path.join(queue_root, self.name)
		os.makedirs(self.folder, exist_ok=True)
		self.wake = threading.Event()
		self.lock = threading.Lock()
		self.last_ok = None
		self.last_error = None
		self.last_heartbeat = 0.0

	def headers(self, headers):
		"""The device's headers plus what this ERPNext needs. Never sent to BioTime."""
		out = dict(headers)
		if self.token:
			out[TOKEN_HEADER] = self.token
		if self.site:
			out["X-Frappe-Site-Name"] = self.site
		return out

	def names(self):
		return sorted(n for n in os.listdir(self.folder) if n.endswith(".json"))

	def put(self, name, data):
		path = os.path.join(self.folder, name)
		if not os.path.exists(path):
			write_file(path, data)

	def deliver(self, name):
		"""True only when ERPNext confirms the upload with "OK"."""
		with open(os.path.join(self.folder, name), encoding="utf-8") as f:
			item = json.load(f)
		body = base64.b64decode(item["body"])
		try:
			status, _, data = send(
				self.url, "POST", item["path_qs"], body, self.headers(item["headers"]), CFG["erpnext_timeout"]
			)
		except Exception as e:
			return False, f"unreachable: {e}"
		text = data.decode("utf-8", "replace").strip()
		if 200 <= status < 300 and text.upper().startswith("OK"):
			return True, text
		return False, f"HTTP {status}: {text[:200]}"

	def drain_forever(self):
		"""Deliver this target's queue in order; on the first failure wait and try again later."""
		while True:
			self.wake.wait(CFG["retry_seconds"])
			self.wake.clear()
			try:
				for name in self.names():
					ok, detail = self.deliver(name)
					if not ok:
						with self.lock:
							self.last_error = f"{now_text()} {detail}"
						LOG.warning("[%s] not reached, %s waiting: %s", self.name, len(self.names()), detail)
						break
					os.remove(os.path.join(self.folder, name))
					with self.lock:
						self.last_ok, self.last_error = now_text(), None
					LOG.info("[%s] took %s: %s", self.name, name, detail)
			except Exception:
				LOG.exception("[%s] queue delivery failed", self.name)

	def copy_heartbeat(self, path_qs, headers):
		"""Let this ERPNext see now and then that the device is alive. Best effort, never queued.

		Sent only while this target's queue is empty: ERPNext takes a heartbeat as "every
		punch up to now has arrived" (it moves Shift Type Last Sync of Checkin), which is
		only true when nothing waits for it."""
		now = time.time()
		if now - self.last_heartbeat < CFG["heartbeat_copy_seconds"] or self.names():
			return
		self.last_heartbeat = now

		def run():
			try:
				send(self.url, "GET", path_qs, b"", self.headers(headers), CFG["erpnext_timeout"])
			except Exception:
				pass

		threading.Thread(target=run, daemon=True).start()

	def status(self):
		with self.lock:
			return {
				"name": self.name,
				"url": self.url,
				"site": self.site or None,
				"token_set": bool(self.token),
				"last_ok": self.last_ok,
				"last_error": self.last_error,
				"queue_waiting": len(self.names()),
			}


def make_upload(path_qs, body, headers):
	"""(file name, file contents) for one attendance upload; the name sorts by arrival."""
	name = f"{time.time():.6f}-{uuid.uuid4().hex[:8]}.json"
	item = {
		"saved": now_text(),
		"path_qs": path_qs,
		"headers": headers,
		"body": base64.b64encode(body).decode("ascii"),
	}
	return name, json.dumps(item).encode("utf-8")


def adopt_old_queue():
	"""Uploads left directly in the queue folder by an older version (one ERPNext only):
	archive them and hand them to every target, so none is lost by the update."""
	root = CFG["queue_dir"]
	for name in sorted(n for n in os.listdir(root) if n.endswith(".json")):
		path = os.path.join(root, name)
		with open(path, "rb") as f:
			data = f.read()
		ARCHIVE.save(name, data)
		for target in TARGETS:
			target.put(name, data)
		os.remove(path)
		LOG.info("moved %s from the old queue to every target", name)


def replay(since=None, target_names=None):
	"""Put archived uploads back in the queue of the chosen targets (all when none given)."""
	chosen = [t for t in TARGETS if not target_names or t.name in target_names]
	unknown = set(target_names or []) - {t.name for t in TARGETS}
	if unknown:
		raise SystemExit(f"unknown target(s): {', '.join(sorted(unknown))}; known: {', '.join(t.name for t in TARGETS)}")
	added = 0
	for name, path in ARCHIVE.items(since):
		with open(path, "rb") as f:
			data = f.read()
		for target in chosen:
			if not os.path.exists(os.path.join(target.folder, name)):
				target.put(name, data)
				added += 1
	return added, chosen


class Handler(BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.1"
	server_version = "ZKTRelay/2.0"
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
				info["biotime_url"] = CFG["biotime_url"]
				info["targets"] = [t.status() for t in TARGETS]
				info["queue_waiting"] = sum(t["queue_waiting"] for t in info["targets"])
				return self.reply(200, json.dumps(info, indent=1).encode(), "application/json")

			if not (path == "/iclock" or path.startswith("/iclock/")):
				return self.reply(404, b"Not found")

			args = {k.lower(): v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
			serial = (args.get("sn") or "").strip().upper()
			endpoint = path[len("/iclock"):].strip("/").lower()
			path_qs = path + ("?" + parts.query if parts.query else "")
			headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
			ours = serial in CFG["serials"]
			STATE.set(device_last_seen=now_text(), device_ip=self.client_address[0], device_last_path=path_qs[:120])

			# Save the ERPNext copies BEFORE BioTime: if anything fails after this, the
			# device sends the same punches again and ERPNext skips the ones it has.
			is_attlog = self.command == "POST" and endpoint.startswith("cdata") and (
				(args.get("table") or "").upper() == "ATTLOG"
			)
			if ours and is_attlog:
				name, data = make_upload(path_qs, body, headers)
				ARCHIVE.save(name, data)
				for target in TARGETS:
					target.put(name, data)
					target.wake.set()
				STATE.bump("attlog_uploads")

			try:
				status, content_type, data = send(
					CFG["biotime_url"], self.command, path_qs, body, headers, CFG["forward_timeout"]
				)
			except Exception as e:
				STATE.set(biotime_last_error=f"{now_text()} {e}")
				LOG.error("BioTime unreachable for %s %s: %s", self.command, path_qs, e)
				return self.reply(502, b"BioTime unreachable")  # the device retries later

			STATE.set(biotime_last_status=status, biotime_last_time=now_text(), biotime_last_error=None)
			if ours and endpoint.startswith("getrequest"):
				for target in TARGETS:
					target.copy_heartbeat(path_qs, headers)
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
	global CFG, TARGETS, ARCHIVE
	parser = argparse.ArgumentParser(description="ZKTeco push relay: device -> BioTime + ERPNext")
	parser.add_argument(
		"--replay-archive",
		action="store_true",
		help="put archived uploads back in the queue (the running relay sends them within a minute)",
	)
	parser.add_argument("--since", help="with --replay-archive: only uploads saved on or after this date (YYYY-MM-DD)")
	parser.add_argument(
		"--target",
		action="append",
		help="with --replay-archive: only this ERPNext target (its name in config.json); can be repeated",
	)
	options = parser.parse_args()

	CFG = load_config()
	os.makedirs(CFG["queue_dir"], exist_ok=True)
	ARCHIVE = Archive(CFG["archive_dir"])
	TARGETS = [Target(conf, CFG["queue_dir"]) for conf in CFG["targets"]]

	if options.replay_archive:
		since = datetime.datetime.strptime(options.since, "%Y-%m-%d").date() if options.since else None
		added, chosen = replay(since, options.target)
		waiting = ", ".join(f"{t.name}: {len(t.names())}" for t in chosen) or "no targets"
		print(f"{added} archived upload(s) put back in the queue; now waiting - {waiting}.")
		return

	setup_logging(CFG["log_file"])
	if not CFG["biotime_url"]:
		LOG.error("biotime_url is not set in config.json: the relay needs the server the device fed before")
		return
	adopt_old_queue()
	for target in TARGETS:
		threading.Thread(target=target.drain_forever, daemon=True).start()
		target.wake.set()  # deliver anything left in its queue from before a restart
	server = ThreadingHTTPServer((CFG["listen_host"], CFG["listen_port"]), Handler)
	server.daemon_threads = True
	LOG.info(
		"relay started on %s:%s -> BioTime %s, ERPNext targets %s, serials %s",
		CFG["listen_host"],
		CFG["listen_port"],
		CFG["biotime_url"],
		", ".join(f"{t.name}={t.url}" for t in TARGETS) or "(none: uploads only archived)",
		sorted(CFG["serials"]),
	)
	server.serve_forever()


if __name__ == "__main__":
	main()
