import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from cargo.telemetry import error_logs
from cargo.telemetry.error_logs import CURSOR_KEY, get_new_error_logs, send_new_error_logs, to_log_line

TRACEBACK = 'Traceback (most recent call last):\n  File "app.py", line 1\nValueError: bad image\n'
METRICS_INFO = {"endpoint": "https://datum.test/", "token": "datum-jwt"}


class IntegrationTestErrorLogPublisher(IntegrationTestCase):
	"""What Datum learns about Cargo Error Logs each minute."""

	def setUp(self):
		frappe.cache.delete_value(CURSOR_KEY)
		self.addCleanup(frappe.cache.delete_value, CURSOR_KEY)

	def test_a_log_line_keeps_the_form_data_out(self):
		row = frappe._dict(
			name="error-1",
			creation=datetime(2026, 11, 14, 5, 0),
			method="cargo.image_builder.build",
			error=TRACEBACK,
			fingerprint="f1",
			trace_id="",
			metadata=json.dumps(
				{
					"type": "background_job",
					"job_name": "cargo.image_builder.build",
					"form_dict": {"key": "secret"},
				}
			),
		)

		line = to_log_line(row)

		self.assertEqual(line["product"], "cargo")
		self.assertEqual(line["attributes"]["exception"], "ValueError")
		self.assertEqual(line["attributes"]["unhandled"], "1")
		self.assertNotIn("secret", json.dumps(line))

	def test_an_old_iso_cursor_still_finds_new_error_logs(self):
		frappe.cache.set_value(CURSOR_KEY, "2020-01-01T00:00:00")
		name = frappe.log_error("Cargo telemetry cursor test").name

		self.assertIn(name, [row.name for row in get_new_error_logs()])

	def test_the_cursor_moves_only_when_datum_takes_the_lines(self):
		frappe.log_error("Cargo telemetry push test")
		with (
			patch("cargo.telemetry.datum.get_metrics_info", return_value=METRICS_INFO),
			patch("cargo.telemetry.datum.requests.post", side_effect=requests.ConnectionError("down")),
			patch.object(error_logs.frappe, "log_error") as log_error,
		):
			send_new_error_logs()

		first_cursor = frappe.cache.get_value(CURSOR_KEY)
		log_error.assert_not_called()

		with (
			patch("cargo.telemetry.datum.get_metrics_info", return_value=METRICS_INFO),
			patch("cargo.telemetry.datum.requests.post", side_effect=requests.ConnectionError("down")),
		):
			send_new_error_logs()

		self.assertEqual(frappe.cache.get_value(CURSOR_KEY), first_cursor)

		with (
			patch("cargo.telemetry.datum.get_metrics_info", return_value=METRICS_INFO),
			patch("cargo.telemetry.datum.requests.post", return_value=MagicMock()) as post,
		):
			send_new_error_logs()

		self.assertEqual(post.call_args.args[0], "https://datum.test/v1/logs/ingest")
		self.assertGreater(frappe.cache.get_value(CURSOR_KEY), first_cursor)

	def test_nothing_is_sent_without_datum_credentials(self):
		frappe.log_error("Cargo telemetry credential test")
		with (
			patch("cargo.telemetry.datum.get_metrics_info", side_effect=RuntimeError),
			patch("cargo.telemetry.datum.requests.post") as post,
		):
			send_new_error_logs()

		post.assert_not_called()
