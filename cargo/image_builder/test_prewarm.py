import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class TestPrewarm(unittest.TestCase):
	def setUp(self):
		self.temporary = tempfile.TemporaryDirectory()
		self.addCleanup(self.temporary.cleanup)
		self.root = Path(self.temporary.name)
		self.marker = self.root / "pending"
		self.marker.touch()
		self.fail_requests = False
		self.fail_logout = False
		self.logouts = 0
		test = self

		class Handler(BaseHTTPRequestHandler):
			def do_GET(self):
				self.respond()

			def do_POST(self):
				self.respond()

			def respond(self):
				status = 200
				body = b'pong\nfrappe.csrf_token = "audit-csrf";'
				if self.path.endswith("/login") and self.command == "POST":
					status = 201
					body = json.dumps({"url": "http://site.local/desk?sid=audit-session"}).encode()
				if self.path.endswith("/logout"):
					test.logouts += 1
					if test.fail_logout or self.headers.get("X-Frappe-CSRF-Token") != "audit-csrf":
						status = 403
				self.send_response(500 if test.fail_requests else status)
				self.end_headers()
				self.wfile.write(body)

			def log_message(self, *_args):
				pass

		self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
		thread = threading.Thread(target=self.server.serve_forever, daemon=True)
		thread.start()
		self.addCleanup(self.server.server_close)
		self.addCleanup(self.server.shutdown)
		for name, program in {
			"su": "printf audit-token",
			"logger": "exit 0",
			"sleep": "exit 0",
			"sync": "exit 0",
		}.items():
			path = self.root / name
			path.write_text("#!/bin/sh\n" + program + "\n")
			path.chmod(0o700)

	def run_prewarm(self):
		source = (Path(__file__).parent / "conf/pilot/provision.sh").read_text()
		body = source.split('cat > "$PREWARM_SCRIPT" <<PREWARM_SCRIPT\n', 1)[1].split(
			"\nPREWARM_SCRIPT\n", 1
		)[0]
		variables = (
			"SITE=site.local\nBENCH=default-bench\nIMAGE_TYPE=Site\nBENCH_USER=frappe\n"
			f"PREWARM_MARKER={self.marker}\nADMIN_UPSTREAM=http://127.0.0.1:{self.server.server_port}\n"
		)
		generated = subprocess.check_output(
			["bash", "-c", variables + "cat <<PREWARM_SCRIPT\n" + body + "\nPREWARM_SCRIPT\n"], text=True
		)
		generated = generated.replace("http://127.0.0.1:8000", f"http://127.0.0.1:{self.server.server_port}")
		generated = generated.replace("attempts=12", "attempts=1")
		return subprocess.run(
			["bash"],
			input=generated,
			text=True,
			capture_output=True,
			timeout=10,
			env={**os.environ, "PATH": str(self.root) + ":" + os.environ["PATH"]},
		)

	def test_failure_keeps_the_marker_and_returns_failure(self):
		self.fail_requests = True
		self.assertNotEqual(self.run_prewarm().returncode, 0)
		self.assertTrue(self.marker.exists())

	def test_retry_warms_the_login_path_and_logs_out_before_completion(self):
		self.fail_requests = True
		self.run_prewarm()
		self.fail_requests = False
		result = self.run_prewarm()
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertFalse(self.marker.exists())
		self.assertEqual(self.logouts, 1)

	def test_failed_logout_does_not_publish_completion(self):
		self.fail_logout = True
		self.assertNotEqual(self.run_prewarm().returncode, 0)
		self.assertTrue(self.marker.exists())

	def test_completed_warmup_does_not_create_another_session(self):
		self.assertEqual(self.run_prewarm().returncode, 0)
		self.assertEqual(self.run_prewarm().returncode, 0)
		self.assertEqual(self.logouts, 1)


class TestProvisionScript(unittest.TestCase):
	def setUp(self):
		self.source = (Path(__file__).parent / "conf/pilot/provision.sh").read_text()

	def test_flushes_the_disk_before_the_snapshot(self):
		self.assertEqual(self.source.rstrip().splitlines()[-1], "sync")

	def test_idle_restart_waits_past_the_warm_capture(self):
		self.assertIn('IDLE_RESTART_SECONDS="${IDLE_RESTART_SECONDS:-900}"', self.source)
		self.assertLess(
			self.source.index("restart_idle_seconds = $IDLE_RESTART_SECONDS"),
			self.source.index("setup production"),
		)
