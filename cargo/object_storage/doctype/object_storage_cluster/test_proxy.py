import os
from unittest.mock import patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from cargo.client_models import GATEWAY, PROXY
from cargo.object_storage.doctype.object_storage_cluster.proxy import CADDY_CONF, Proxy
from cargo.testing import SETTINGS, use_test_settings

GATEWAY_ADDRESS = "fdaa:1::10"
PROXY_ADDRESS = "fdaa:1::20"
PUBLIC_IPV4 = "203.0.113.10"


class IntegrationTestProxy(IntegrationTestCase):
	"""Caddy on the proxy node routes the public S3 domains to the gateway."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		frappe.db.set_single_value("Cargo Settings", "central_url", "https://central.test/dashboard")
		self.cluster = frappe.get_doc({"doctype": "Object Storage Cluster"}).insert()
		self.add_machine(GATEWAY, GATEWAY_ADDRESS)
		self.proxy_machine = self.add_machine(PROXY, PROXY_ADDRESS, public_ipv4=PUBLIC_IPV4)

	def add_machine(self, role: str, address: str, public_ipv4: str | None = None) -> str:
		machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": self.cluster.doctype,
				"reference_name": self.cluster.name,
				"role": role,
				"vm_id": f"vm-{role}-{frappe.generate_hash(length=8)}",
				"disk_size_gb": 20,
				"address": address,
				"public_ipv4": public_ipv4,
				"status": "Running",
			}
		).insert()
		self.cluster.append("machines", {"machine": machine.name, "role": role})
		self.cluster.save()

		return machine.name

	def proxy(self) -> Proxy:
		return Proxy(frappe.get_doc("Object Storage Cluster", self.cluster.name))

	def test_upstreams_are_the_gateway_garage_ports(self):
		environment = self.proxy().environment

		self.assertEqual(environment["WILDCARD_DOMAIN"], SETTINGS["wildcard_domain"])
		self.assertEqual(environment["S3_UPSTREAM"], f"[{GATEWAY_ADDRESS}]:{self.cluster.s3_port}")
		self.assertEqual(environment["ADMIN_UPSTREAM"], f"[{GATEWAY_ADDRESS}]:{self.cluster.admin_port}")

	def test_s3_accepts_browser_calls_from_the_central_origin_only(self):
		self.assertEqual(self.proxy().environment["CORS_ORIGIN"], "https://central.test")

	def test_a_proxy_cannot_be_set_up_without_a_central_url(self):
		frappe.db.set_single_value("Cargo Settings", "central_url", "")

		with self.assertRaisesRegex(frappe.ValidationError, "Central URL"):
			self.proxy().environment

	def test_a_proxy_cannot_be_set_up_without_a_domain(self):
		frappe.db.set_single_value("Cargo Settings", "wildcard_domain", "")

		with self.assertRaisesRegex(frappe.ValidationError, "Wildcard Domain"):
			self.proxy().environment

	def test_the_caddy_script_runs_on_the_proxy_node(self):
		with patch("cargo.object_storage.doctype.object_storage_cluster.proxy.run_over_ssh") as run:
			self.proxy().install()

		address, script = run.call_args.args[:2]
		self.assertEqual(address, PROXY_ADDRESS)
		self.assertIn("s3-svc.${WILDCARD_DOMAIN} {", script)
		self.assertIn("s3-admin-svc.${WILDCARD_DOMAIN} {", script)
		self.assertIn("reverse_proxy ${S3_UPSTREAM}", script)
		self.assertIn(f"export S3_UPSTREAM='[{GATEWAY_ADDRESS}]:{self.cluster.s3_port}'", script)

	def test_a_proxy_without_a_public_address_is_refused(self):
		frappe.db.set_value("Machine", self.proxy_machine, "public_ipv4", None)

		with (
			patch("cargo.object_storage.doctype.object_storage_cluster.proxy.run_over_ssh") as run,
			self.assertRaisesRegex(frappe.ValidationError, "public IPv4"),
		):
			self.proxy().install()

		run.assert_not_called()

	def test_a_dead_proxy_is_not_the_cluster_proxy(self):
		frappe.db.set_value("Machine", self.proxy_machine, "status", "Terminated")

		self.assertIsNone(frappe.get_doc("Object Storage Cluster", self.cluster.name).proxy_node)

	def probe(self, admin_ok: bool = True, s3_status: int = 403):
		with patch("requests.get") as get, patch("requests.head") as head:
			get.return_value.ok = admin_ok
			head.return_value.status_code = s3_status
			serving = self.proxy().is_serving

		return serving, get, head

	def test_serving_means_both_domains_answer_over_https(self):
		serving, get, head = self.probe()

		self.assertTrue(serving)
		domain = SETTINGS["wildcard_domain"]
		self.assertEqual(get.call_args.args[0], f"https://s3-admin-svc.{domain}/health")
		self.assertEqual(head.call_args.args[0], f"https://s3-svc.{domain}/")

	def test_s3_refusing_an_unsigned_call_still_counts_as_serving(self):
		for status in (403, 404):
			with self.subTest(status=status):
				self.assertTrue(self.probe(s3_status=status)[0])

	def test_a_gateway_caddy_cannot_reach_is_not_serving(self):
		self.assertFalse(self.probe(s3_status=502)[0])

	def test_an_unhealthy_admin_api_is_not_serving(self):
		self.assertFalse(self.probe(admin_ok=False)[0])

	def test_an_unreachable_domain_is_not_serving(self):
		with patch("requests.get", side_effect=requests.ConnectionError("no route")):
			self.assertFalse(self.proxy().is_serving)

	def test_an_invalid_certificate_is_not_serving(self):
		with patch("requests.get", side_effect=requests.exceptions.SSLError("self-signed")):
			self.assertFalse(self.proxy().is_serving)

	def test_the_script_ships_with_the_app(self):
		self.assertTrue(os.path.isfile(frappe.get_app_path("cargo", *CADDY_CONF)))
