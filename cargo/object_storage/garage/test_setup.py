import os
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.client_models import GATEWAY, STORAGE
from cargo.object_storage.garage.setup import NGINX_CONF, Setup
from cargo.testing import SETTINGS, use_test_settings


def machine(role: str) -> dict:
	return {
		"name": f"OSC-0001-{role}-0001",
		"role": role,
		"zone": "z1",
		"address": "fdaa:1::5",
		"disk_size_gb": 20,
	}


class IntegrationTestGatewayNginx(IntegrationTestCase):
	"""The gateway answers on port 80 by subdomain, in front of Garage's own ports."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		self.setup = Setup(frappe.get_doc({"doctype": "Object Storage Cluster"}).insert())

	def test_the_subdomains_hang_off_the_configured_domain(self):
		environment = self.setup.nginx_environment()

		self.assertEqual(environment["WILDCARD_DOMAIN"], SETTINGS["wildcard_domain"])
		self.assertEqual(environment["S3_PORT"], self.setup.cluster.s3_port)
		self.assertEqual(environment["ADMIN_PORT"], self.setup.cluster.admin_port)

	def test_only_the_mesh_and_this_machine_may_name_the_client(self):
		proxies = self.setup.nginx_environment()["TRUSTED_PROXIES"].split()

		self.assertIn("fd00::/8", proxies)
		self.assertNotIn("0.0.0.0/0", proxies)
		self.assertNotIn("::/0", proxies)

	def test_a_gateway_cannot_be_routed_without_a_domain(self):
		frappe.db.set_single_value("Cargo Settings", "wildcard_domain", "")

		with self.assertRaisesRegex(frappe.ValidationError, "Wildcard Domain"):
			Setup(self.setup.cluster).nginx_environment()

	def test_garage_is_not_told_any_domain(self):
		"""Buckets are addressed by path, so Garage has no hostname to strip one from."""
		environment = self.setup.install_environment(machine(STORAGE))

		self.assertNotIn("WILDCARD_DOMAIN", environment)
		self.assertNotIn("BASE_DOMAIN", environment)

	def test_the_nginx_script_is_the_one_run(self):
		with patch.object(Setup, "run") as run:
			self.setup.setup_nginx_on_machine(machine(GATEWAY))

		self.assertIn("server_name s3-svc.${WILDCARD_DOMAIN};", run.call_args.args[1])
		self.assertNotIn("*.s3-svc", run.call_args.args[1])
		self.assertIn("server_name s3-admin-svc.${WILDCARD_DOMAIN}", run.call_args.args[1])
		self.assertIn(f"export WILDCARD_DOMAIN={SETTINGS['wildcard_domain']}", run.call_args.args[1])

	def test_only_the_gateway_gets_nginx(self):
		for role, expected in ((GATEWAY, 1), (STORAGE, 0)):
			with (
				self.subTest(role=role),
				patch.object(Setup, "run"),
				patch.object(Setup, "node_identifier", return_value="id@[fdaa:1::5]:3901"),
				patch.object(Setup, "connect_nodes"),
				patch.object(Setup, "stage_role"),
				patch.object(Setup, "setup_nginx_on_machine") as nginx,
			):
				self.setup.setup_machine(machine(role))

				self.assertEqual(nginx.call_count, expected)

	def test_the_script_ships_with_the_app(self):
		self.assertTrue(os.path.isfile(frappe.get_app_path("cargo", *NGINX_CONF)))
