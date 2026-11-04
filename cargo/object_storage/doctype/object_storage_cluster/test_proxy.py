import os
from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.client_models import GATEWAY
from cargo.object_storage.doctype.object_storage_cluster.proxy import Proxy
from cargo.testing import SETTINGS, use_test_settings

GATEWAY_ADDRESS = "fdaa:1::10"
PUBLIC_IPV4 = "203.0.113.10"
PROXY_MODULE = "cargo.object_storage.doctype.object_storage_cluster.proxy"


class IntegrationTestProxy(IntegrationTestCase):
	"""Caddy on the gateway routes the public S3 domains to Garage on its mesh address."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		frappe.db.set_single_value("Cargo Settings", "central_url", "https://central.test/dashboard")
		self.cluster = frappe.get_doc({"doctype": "Object Storage Cluster"}).insert()
		self.gateway = self.add_machine(GATEWAY, GATEWAY_ADDRESS, public_ipv4=PUBLIC_IPV4)

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

	def test_the_caddyfile_runs_on_the_gateway(self):
		with patch("cargo.caddy.run_over_ssh") as run:
			self.proxy().install()

		address, sent = run.call_args.args[:2]
		domain = SETTINGS["wildcard_domain"]
		self.assertEqual(address, GATEWAY_ADDRESS)
		self.assertIn(f"s3-svc.{domain} {{", sent)
		self.assertIn(f"s3-admin-svc.{domain} {{", sent)
		self.assertIn(f"reverse_proxy [{GATEWAY_ADDRESS}]:{self.cluster.s3_port}", sent)
		self.assertIn('header_down Access-Control-Allow-Origin "https://central.test"', sent)

	def test_a_gateway_without_a_public_address_is_refused(self):
		frappe.db.set_value("Machine", self.gateway, "public_ipv4", None)

		with (
			patch("cargo.caddy.run_over_ssh") as run,
			self.assertRaisesRegex(frappe.ValidationError, "public IPv4"),
		):
			self.proxy().install()

		run.assert_not_called()

	def test_a_stopped_gateway_is_refused(self):
		frappe.db.set_value("Machine", self.gateway, "status", "Stopped")

		with self.assertRaisesRegex(frappe.ValidationError, "gateway is not running"):
			self.proxy().machine

	def probe(self, admin: Mock | None = None, s3: Mock | None = None) -> tuple[bool, list]:
		"""`is_serving` against the given answers, None meaning the request never got one."""
		domain = SETTINGS["wildcard_domain"]
		replies = {f"https://s3-admin-svc.{domain}/health": admin, f"https://s3-svc.{domain}/": s3}
		with patch(f"{PROXY_MODULE}.answers", side_effect=lambda url, **_: replies[url]) as sent:
			serving = self.proxy().is_serving

		return serving, sent.call_args_list

	def test_serving_means_both_domains_answer_over_https(self):
		serving, calls = self.probe(admin=Mock(ok=True), s3=Mock(status_code=403))

		self.assertTrue(serving)
		domain = SETTINGS["wildcard_domain"]
		self.assertEqual(calls[0].args, (f"https://s3-admin-svc.{domain}/health",))
		self.assertEqual(calls[1].args, (f"https://s3-svc.{domain}/",))
		self.assertEqual(calls[1].kwargs, {"method": "HEAD"})

	def test_s3_refusing_an_unsigned_call_still_counts_as_serving(self):
		for status in (403, 404):
			with self.subTest(status=status):
				self.assertTrue(self.probe(admin=Mock(ok=True), s3=Mock(status_code=status))[0])

	def test_a_gateway_caddy_cannot_reach_is_not_serving(self):
		self.assertFalse(self.probe(admin=Mock(ok=True), s3=Mock(status_code=502))[0])

	def test_an_unhealthy_admin_api_is_not_serving(self):
		self.assertFalse(self.probe(admin=Mock(ok=False), s3=Mock(status_code=403))[0])

	def test_a_domain_that_never_answers_is_not_serving(self):
		self.assertFalse(self.probe(admin=None, s3=None)[0])

	def test_the_caddyfile_ships_with_the_app(self):
		self.assertTrue(os.path.isfile(frappe.get_app_path("cargo", *Proxy.caddyfile)))
