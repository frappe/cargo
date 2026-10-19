from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from cargo.client_models import TELEMETRY
from cargo.telemetry.health import live
from cargo.telemetry.health.live import ping_host_machine
from cargo.testing import use_test_settings


class IntegrationTestPingHostMachine(IntegrationTestCase):
	"""Reading the datum host through its own nginx, and what its status says after."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": "Datum Server",
				"reference_name": "Datum Server",
				"role": TELEMETRY,
				"disk_size_gb": 20,
				"vm_id": f"vm-{frappe.generate_hash(length=8)}",
				"address": "fdaa:1::5",
				"status": "Running",
			}
		).insert()
		self.server = frappe.get_single("Datum Server")
		self.server.update(
			{
				"repository": "https://github.com/frappe/datum",
				"branch": "develop",
				"machine": machine.name,
				"status": "Active",
				"error": None,
			}
		)
		with patch("cargo.telemetry.doctype.datum_server.datum_server.configure_telemetry_webhook"):
			self.server.save()
		self.addCleanup(frappe.db.rollback)

	def response(self, status: int = 200) -> MagicMock:
		reply = MagicMock(status_code=status)
		if status >= 400:
			reply.raise_for_status.side_effect = requests.HTTPError(f"{status} Server Error")
		return reply

	def ping(self, *replies) -> MagicMock:
		"""Ping with datum answering first and ClickHouse second, returning the recorder."""
		request = MagicMock(side_effect=list(replies) or [self.response(), self.response()])
		with patch.object(live.requests, "request", request):
			ping_host_machine()
		self.server.reload()

		return request

	def test_both_answering_leaves_it_active(self):
		self.ping()

		self.assertEqual(self.server.status, "Active")

	def test_datum_not_answering_marks_it_unreachable(self):
		self.ping(requests.ConnectionError("refused"))

		self.assertEqual(self.server.status, "Unreachable")
		self.assertIn("datum did not answer", self.server.error)

	def test_clickhouse_failing_a_read_marks_it_unreachable(self):
		"""The SIGILL case: nginx is up and datum answers, ClickHouse behind it does not."""
		self.ping(self.response(), self.response(502))

		self.assertEqual(self.server.status, "Unreachable")
		self.assertIn("ClickHouse did not answer a read", self.server.error)

	def test_it_turns_active_again_once_both_answer(self):
		self.ping(requests.ConnectionError("refused"))
		self.ping()

		self.assertEqual(self.server.status, "Active")
		self.assertFalse(self.server.error)

	def test_an_unreachable_host_is_not_saved_again_for_the_same_reason(self):
		"""Every save fires the webhook to Central."""
		self.ping(requests.ConnectionError("refused"))
		modified = self.server.modified
		self.ping(requests.ConnectionError("refused"))

		self.assertEqual(self.server.modified, modified)

	def test_a_host_still_setting_up_or_failed_is_not_pinged(self):
		"""Failed is the spawner's to retry, and Setting Up belongs to the setup run."""
		for status in ("Setting Up", "Failed", "Draft"):
			self.server.db_set("status", status)
			request = self.ping()

			request.assert_not_called()
			self.assertEqual(self.server.status, status)

	def test_it_reads_through_nginx_as_the_insights_user(self):
		request = self.ping()
		datum, clickhouse = request.call_args_list
		domain = self.server.wildcard_domain

		self.assertEqual(datum.args, ("GET", "http://[fdaa:1::5]:80/health"))
		self.assertEqual(datum.kwargs["headers"]["Host"], f"telemetry-svc.{domain}")
		self.assertEqual(clickhouse.args, ("POST", "http://[fdaa:1::5]:80/"))
		self.assertEqual(clickhouse.kwargs["headers"]["Host"], f"telemetry-read-svc.{domain}")
		self.assertEqual(clickhouse.kwargs["headers"]["X-ClickHouse-User"], "insights")
		self.assertEqual(clickhouse.kwargs["data"], live.READ_PROBE)
		self.assertEqual(clickhouse.kwargs["timeout"], live.PING_TIMEOUT)
