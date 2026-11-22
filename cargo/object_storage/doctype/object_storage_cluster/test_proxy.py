import os
from unittest.mock import Mock, patch

import frappe
from cloudtoolkit.ssh.client import SSHClient
from frappe.tests import IntegrationTestCase

from cargo.client_models import GATEWAY
from cargo.object_storage.doctype.object_storage_cluster.proxy import Proxy
from cargo.testing import SETTINGS, nginx_over_ssh, use_test_settings

GATEWAY_ADDRESS = "fdaa:1::10"
PUBLIC_IPV4 = "203.0.113.10"
SECOND_GATEWAY_ADDRESS = "fdaa:1::11"
SECOND_PUBLIC_IPV4 = "203.0.113.11"
PROXY_MODULE = "cargo.object_storage.doctype.object_storage_cluster.proxy"


class IntegrationTestProxy(IntegrationTestCase):
	"""nginx on each gateway routes the public S3 domains to that gateway's Garage on its mesh
	address, and DNS names the first gateway."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		# Other suites commit clusters that serve or set up, which would block publishing DNS here.
		frappe.db.set_value(
			"Object Storage Cluster", {"status": ("in", ("Active", "Setting Up"))}, "status", "Draft"
		)
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
		proxy = self.proxy()
		context = proxy.get_context(proxy.machines[0])

		self.assertEqual(context["domain"], SETTINGS["wildcard_domain"])
		self.assertEqual(context["s3_upstream"], f"[{GATEWAY_ADDRESS}]:{self.cluster.s3_port}")
		self.assertEqual(context["admin_upstream"], f"[{GATEWAY_ADDRESS}]:{self.cluster.admin_port}")

	def test_a_proxy_cannot_be_set_up_without_a_domain(self):
		frappe.db.set_single_value("Cargo Settings", "wildcard_domain", "")

		with self.assertRaisesRegex(frappe.ValidationError, "Wildcard Domain"):
			proxy = self.proxy()
			proxy.get_context(proxy.machines[0])

	def test_the_config_runs_on_the_gateway(self):
		with (
			patch("cargo.nginx.AtlasClient"),
			patch("cargo.atlas_client.AtlasClient"),
			nginx_over_ssh() as (_, configs),
		):
			self.proxy().install()

		sent = configs[GATEWAY_ADDRESS]
		domain = SETTINGS["wildcard_domain"]
		self.assertIn(f"server_name s3-svc.{domain};", sent)
		self.assertIn(f"server_name s3-admin-svc.{domain};", sent)
		self.assertIn(f"proxy_pass http://[{GATEWAY_ADDRESS}]:{self.cluster.s3_port};", sent)
		# Garage answers CORS per bucket; a header nginx set would override every bucket's origins.
		self.assertNotIn("Access-Control-Allow-", sent)
		self.assertIn("OPTIONS 3600;", sent)
		self.assertIn("add_header Vary Origin always;", sent)

	def test_every_gateway_routes_to_its_own_garage_and_is_in_dns(self):
		self.add_machine(GATEWAY, SECOND_GATEWAY_ADDRESS, public_ipv4=SECOND_PUBLIC_IPV4)
		with (
			patch("cargo.nginx.AtlasClient") as atlas,
			patch("cargo.atlas_client.AtlasClient"),
			nginx_over_ssh() as (_, sent),
		):
			self.proxy().install()

		self.assertIn(
			f"proxy_pass http://[{SECOND_GATEWAY_ADDRESS}]:{self.cluster.s3_port};",
			sent[SECOND_GATEWAY_ADDRESS],
		)
		self.assertIn(f"proxy_pass http://[{GATEWAY_ADDRESS}]:{self.cluster.s3_port};", sent[GATEWAY_ADDRESS])
		domain = SETTINGS["wildcard_domain"]
		published = {
			call.args[0]: call.args[1]
			for call in atlas.from_settings.return_value.set_dns_records.call_args_list
		}
		self.assertEqual(published[f"s3-svc.{domain}"], [PUBLIC_IPV4, SECOND_PUBLIC_IPV4])
		self.assertEqual(published[f"internal-s3-svc.{domain}"], [GATEWAY_ADDRESS, SECOND_GATEWAY_ADDRESS])

	def test_route53_checks_each_public_gateway_on_the_admin_health_route(self):
		with patch("cargo.nginx.AtlasClient") as atlas:
			proxy = self.proxy()
			proxy.publish_dns(proxy.machines, proxy.domains)

		domain = SETTINGS["wildcard_domain"]
		calls = {
			call.args[0]: call.kwargs
			for call in atlas.from_settings.return_value.set_dns_records.call_args_list
		}
		self.assertEqual(
			calls[f"s3-svc.{domain}"], {"health_check_url": f"https://s3-admin-svc.{domain}/health"}
		)
		self.assertEqual(calls[f"internal-s3-svc.{domain}"], {})

	def test_a_stopped_gateway_leaves_dns(self):
		self.add_machine(GATEWAY, SECOND_GATEWAY_ADDRESS, public_ipv4=SECOND_PUBLIC_IPV4)
		frappe.db.set_value("Machine", self.gateway, "status", "Stopped")

		self.assertEqual([gateway.address for gateway in self.proxy().machines], [SECOND_GATEWAY_ADDRESS])

	def test_a_gateway_without_a_public_address_is_refused(self):
		frappe.db.set_value("Machine", self.gateway, "public_ipv4", None)

		with (
			patch.object(SSHClient, "run", autospec=True) as run,
			self.assertRaisesRegex(frappe.ValidationError, "public IPv4"),
		):
			self.proxy().install()

		run.assert_not_called()

	def test_dns_is_not_published_while_another_cluster_sets_up(self):
		"""Two setups that both passed the form check must not both point DNS at their gateway."""
		frappe.get_doc({"doctype": "Object Storage Cluster"}).insert().db_set("status", "Setting Up")

		with (
			patch("cargo.nginx.AtlasClient") as atlas,
			patch("cargo.atlas_client.AtlasClient"),
			nginx_over_ssh(),
			self.assertRaisesRegex(frappe.ValidationError, "is Setting Up"),
		):
			self.proxy().install()

		atlas.from_settings.return_value.set_dns_records.assert_not_called()

	def test_the_cluster_points_its_names_at_the_gateway_again(self):
		domain = SETTINGS["wildcard_domain"]
		with patch("cargo.nginx.AtlasClient") as atlas, patch.object(SSHClient, "run", autospec=True) as run:
			frappe.get_doc("Object Storage Cluster", self.cluster.name).update_dns_records()

		published = {
			call.args[0]: call.args[1]
			for call in atlas.from_settings.return_value.set_dns_records.call_args_list
		}
		self.assertEqual(published[f"s3-svc.{domain}"], [PUBLIC_IPV4])
		self.assertEqual(published[f"internal-s3-admin-svc.{domain}"], [GATEWAY_ADDRESS])
		run.assert_not_called()

	def test_only_a_writer_can_update_the_dns_records(self):
		cluster = frappe.get_doc("Object Storage Cluster", self.cluster.name)
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")

		with patch("cargo.nginx.AtlasClient") as atlas, self.assertRaises(frappe.PermissionError):
			cluster.update_dns_records()

		atlas.from_settings.assert_not_called()

	def test_a_stopped_gateway_is_refused(self):
		frappe.db.set_value("Machine", self.gateway, "status", "Stopped")

		with self.assertRaisesRegex(frappe.ValidationError, "gateway is not running"):
			self.proxy().machines

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

	def test_a_garage_nginx_cannot_reach_is_not_serving(self):
		self.assertFalse(self.probe(admin=Mock(ok=True), s3=Mock(status_code=502))[0])

	def test_an_unhealthy_admin_api_is_not_serving(self):
		self.assertFalse(self.probe(admin=Mock(ok=False), s3=Mock(status_code=403))[0])

	def test_a_domain_that_never_answers_is_not_serving(self):
		self.assertFalse(self.probe(admin=None, s3=None)[0])

	def test_the_config_ships_with_the_app(self):
		self.assertTrue(self.proxy().template_path.is_file())
