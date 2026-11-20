import os
from unittest.mock import Mock, patch

import frappe
from cloudtoolkit.ssh.client import SSHClient
from frappe.tests import IntegrationTestCase

from cargo.client_models import TELEMETRY
from cargo.telemetry.doctype.datum_server.proxy import DATUM_PORT, Proxy
from cargo.testing import SETTINGS, nginx_over_ssh, use_test_settings

PROXY_MODULE = "cargo.telemetry.doctype.datum_server.proxy"


class IntegrationTestTelemetryProxy(IntegrationTestCase):
	"""nginx on the datum host serves both domains and keeps ClickHouse's own UIs internal."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		self.addCleanup(frappe.db.rollback)
		self.server = frappe.get_doc(
			{
				"doctype": "Datum Server",
				"clickhouse_host": "clickhouse.internal",
				"repository": "https://github.com/frappe/datum",
				"branch": "develop",
			}
		).insert()
		self.machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": self.server.doctype,
				"reference_name": self.server.name,
				"role": TELEMETRY,
				"disk_size_gb": 20,
				"vm_id": f"vm-{frappe.generate_hash(length=8)}",
				"address": "fdaa:1::9",
				"public_ipv4": "203.0.113.20",
				"status": "Running",
			}
		).insert()
		self.server.db_set("machine", self.machine.name)
		self.server.reload()

	def proxy(self) -> Proxy:
		return Proxy(frappe.get_single("Datum Server"))

	def test_datum_and_clickhouse_stay_on_loopback(self):
		proxy = self.proxy()
		context = proxy.get_context(proxy.machines[0])

		self.assertEqual(context["datum_upstream"], f"127.0.0.1:{DATUM_PORT}")
		self.assertEqual(context["clickhouse_upstream"], f"127.0.0.1:{self.server.clickhouse_port}")
		self.assertEqual(context["domain"], SETTINGS["wildcard_domain"])

	def test_the_config_keeps_clickhouse_uis_internal(self):
		proxy = self.proxy()
		rendered = proxy.render(proxy.machines[0])
		domain = SETTINGS["wildcard_domain"]

		self.assertIn(f"server_name telemetry-svc.{domain};", rendered)
		self.assertIn(f"server_name internal-telemetry-svc.{domain};", rendered)
		self.assertIn(f"server_name telemetry-read-svc.{domain};", rendered)
		self.assertIn("location = /ping {", rendered)
		self.assertIn("if ($arg_query) {", rendered)
		self.assertIn("ssl_certificate /etc/nginx/tls/telemetry.crt;", rendered)

	def test_nginx_runs_on_the_datum_host(self):
		with (
			patch("cargo.nginx.AtlasClient"),
			patch("cargo.atlas_client.AtlasClient"),
			nginx_over_ssh() as (_, configs),
		):
			self.proxy().install()

		self.assertEqual(list(configs), ["fdaa:1::9"])

	def test_a_host_without_a_public_address_is_refused(self):
		frappe.db.set_value("Machine", self.machine.name, "public_ipv4", None)

		with (
			patch.object(SSHClient, "run", autospec=True) as run,
			self.assertRaisesRegex(frappe.ValidationError, "public IPv4"),
		):
			self.proxy().install()

		run.assert_not_called()

	def test_the_server_points_its_names_at_the_datum_host_again(self):
		domain = SETTINGS["wildcard_domain"]
		with patch("cargo.nginx.AtlasClient") as atlas, patch.object(SSHClient, "run", autospec=True) as run:
			frappe.get_single("Datum Server").update_dns_records()

		published = {
			call.args[0]: call.args[1]
			for call in atlas.from_settings.return_value.set_dns_records.call_args_list
		}
		self.assertEqual(published[f"telemetry-svc.{domain}"], ["203.0.113.20"])
		self.assertEqual(published[f"internal-telemetry-read-svc.{domain}"], ["fdaa:1::9"])
		run.assert_not_called()

	def test_only_a_writer_can_update_the_dns_records(self):
		server = frappe.get_single("Datum Server")
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")

		with patch("cargo.nginx.AtlasClient") as atlas, self.assertRaises(frappe.PermissionError):
			server.update_dns_records()

		atlas.from_settings.assert_not_called()

	def probe(self, datum: Mock | None, clickhouse: Mock | None) -> bool:
		domain = SETTINGS["wildcard_domain"]
		replies = {
			f"https://telemetry-svc.{domain}/health": datum,
			f"https://telemetry-read-svc.{domain}/ping": clickhouse,
		}
		with patch(f"{PROXY_MODULE}.answers", side_effect=lambda url, **_: replies[url]):
			return self.proxy().is_serving

	def test_serving_means_datum_and_clickhouse_both_answer(self):
		self.assertTrue(self.probe(Mock(ok=True), Mock(ok=True)))

	def test_clickhouse_down_behind_a_healthy_datum_is_not_serving(self):
		self.assertFalse(self.probe(Mock(ok=True), Mock(ok=False)))

	def test_a_domain_that_never_answers_is_not_serving(self):
		self.assertFalse(self.probe(None, Mock(ok=True)))

	def test_the_config_ships_with_the_app(self):
		self.assertTrue(self.proxy().template_path.is_file())
