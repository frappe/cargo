import os
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.client_models import GATEWAY, STORAGE
from cargo.object_storage.doctype.object_storage_cluster.setup import NGINX_CONF, Setup
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

	def test_s3_accepts_browser_calls_from_the_central_origin_only(self):
		frappe.db.set_single_value("Cargo Settings", "central_url", "https://central.test/dashboard")

		self.assertEqual(self.setup.nginx_environment()["CORS_ORIGIN"], "https://central.test")

	def test_a_gateway_cannot_be_routed_without_a_central_url(self):
		frappe.db.set_single_value("Cargo Settings", "central_url", "")

		with self.assertRaisesRegex(frappe.ValidationError, "Central URL"):
			self.setup.nginx_environment()

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
		self.assertIn(
			'add_header Access-Control-Allow-Origin   "${CORS_ORIGIN}" always;', run.call_args.args[1]
		)

	def test_nginx_is_refused_on_a_storage_node(self):
		with (
			patch.object(Setup, "run") as run,
			self.assertRaisesRegex(frappe.ValidationError, "Only the gateway"),
		):
			self.setup.setup_nginx_on_machine(machine(STORAGE))

		run.assert_not_called()

	def test_the_script_ships_with_the_app(self):
		self.assertTrue(os.path.isfile(frappe.get_app_path("cargo", *NGINX_CONF)))


class IntegrationTestStaleRoles(IntegrationTestCase):
	"""A role no machine of the cluster holds any more leaves the layout on the next apply."""

	def setup_with(self, roles: list[tuple[str, str]], up: set[str], staged: list | None = None) -> Setup:
		"""A cluster holding `storage-1` and `storage-2`, whose layout has `roles` as
		(node id, machine) pairs, with the nodes in `up` running."""
		cluster = frappe.get_doc(
			{
				"doctype": "Object Storage Cluster",
				"machines": [
					{"machine": "storage-1", "role": STORAGE},
					{"machine": "storage-2", "role": STORAGE},
				],
			}
		)
		setup = Setup(cluster)
		layout = {
			"version": 4,
			"roles": [{"id": node_id, "tags": [machine]} for node_id, machine in roles],
			"stagedRoleChanges": staged or [],
		}
		nodes = [{"id": node_id, "isUp": node_id in up} for node_id, _ in roles]
		patch.object(Setup, "layout", return_value=layout).start()
		patch.object(Setup, "status", return_value={"nodes": nodes}).start()
		self.assign = patch.object(Setup, "assign_roles").start()
		self.addCleanup(patch.stopall)

		return setup

	def removed(self) -> list[str]:
		removals = self.assign.call_args.args[0]
		return [removal["id"] for removal in removals]

	def test_the_old_node_of_a_machine_that_came_back_new_is_removed(self):
		setup = self.setup_with(
			[("old", "storage-1"), ("new", "storage-1"), ("two", "storage-2")], {"new", "two"}
		)

		self.assertEqual(setup.stage_stale_role_removals(), ["old"])
		self.assertEqual(self.removed(), ["old"])

	def test_a_new_node_still_staged_replaces_the_old_one(self):
		setup = self.setup_with(
			[("old", "storage-1"), ("two", "storage-2")],
			{"two"},
			staged=[{"id": "new", "tags": ["storage-1"]}],
		)
		patch.object(
			Setup,
			"status",
			return_value={"nodes": [{"id": "new", "isUp": True}, {"id": "old", "isUp": False}]},
		).start()

		self.assertEqual(setup.stage_stale_role_removals(), ["old"])

	def test_a_machine_that_is_down_keeps_its_role(self):
		setup = self.setup_with([("one", "storage-1"), ("two", "storage-2")], {"two"})

		self.assertEqual(setup.stage_stale_role_removals(), [])
		self.assign.assert_not_called()

	def test_a_released_machine_leaves_the_layout(self):
		setup = self.setup_with([("one", "storage-1"), ("gone", "storage-3")], {"one"})

		self.assertEqual(setup.stage_stale_role_removals(), ["gone"])

	def test_dead_nodes_are_skipped_when_every_one_was_removed(self):
		setup = self.setup_with(
			[("old", "storage-1"), ("new", "storage-1"), ("two", "storage-2")], {"new", "two"}
		)
		with patch.object(Setup, "skip_dead_nodes") as skip:
			setup.skip_removed_nodes(["old"], 5)

		skip.assert_called_once_with(5)

	def test_dead_nodes_are_not_skipped_while_another_node_is_down(self):
		setup = self.setup_with([("old", "storage-1"), ("new", "storage-1"), ("two", "storage-2")], {"new"})
		with patch.object(Setup, "skip_dead_nodes") as skip:
			setup.skip_removed_nodes(["old"], 5)

		skip.assert_not_called()
