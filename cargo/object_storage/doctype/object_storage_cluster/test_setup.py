import os
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from cargo.client_models import GATEWAY, STORAGE
from cargo.object_storage.doctype.object_storage_cluster.setup import CONF, Setup
from cargo.ssh import script
from cargo.testing import SETTINGS, use_test_settings


def machine(role: str) -> dict:
	return {
		"name": f"OSC-0001-{role}-0001",
		"role": role,
		"zone": "z1",
		"address": "fdaa:1::5",
		"disk_size_gb": 20,
		"vm_id": f"vm-{role}",
	}


class IntegrationTestGarageInstall(IntegrationTestCase):
	"""Garage listens on the node's mesh address only, never on a gateway's public one."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		self.setup = Setup(frappe.get_doc({"doctype": "Object Storage Cluster"}).insert())

	def test_garage_binds_to_the_mesh_address_only(self):
		cluster = self.setup.cluster
		environment = self.setup.install_environment(machine(GATEWAY))

		self.assertEqual(environment["RPC_BIND_ADDR"], f"[fdaa:1::5]:{cluster.rpc_port}")
		self.assertEqual(environment["S3_BIND_ADDR"], f"[fdaa:1::5]:{cluster.s3_port}")
		self.assertEqual(environment["ADMIN_BIND_ADDR"], f"[fdaa:1::5]:{cluster.admin_port}")

	def test_the_install_script_binds_no_wildcard_address(self):
		body = script(*CONF, "install.sh")

		self.assertNotIn("[::]", body)
		self.assertIn('api_bind_addr = "$S3_BIND_ADDR"', body)

	def test_garage_is_not_told_any_domain(self):
		"""Buckets are addressed by path, so Garage has no hostname to strip one from."""
		environment = self.setup.install_environment(machine(STORAGE))

		self.assertNotIn("WILDCARD_DOMAIN", environment)
		self.assertNotIn("BASE_DOMAIN", environment)


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

	def skip_with(self, setup: Setup, nodes: list[dict], is_draining: bool = True):
		"""Run the skip with Garage reporting `nodes`, returning the mocked skip call."""
		with (
			patch.object(Setup, "status", return_value={"nodes": nodes}),
			patch.object(Setup, "is_draining", return_value=is_draining),
			patch.object(Setup, "layout_version", return_value=5),
			patch.object(Setup, "skip_dead_nodes") as skip,
		):
			setup.skip_dead_nodes_left_out_of_layout()

		return skip

	def test_a_dead_node_removed_by_an_earlier_run_is_still_skipped(self):
		"""The run that removed it may have failed to skip, so every run checks again."""
		setup = self.setup_with([("new", "storage-1"), ("two", "storage-2")], {"new", "two"})
		nodes = [{"id": "new", "isUp": True}, {"id": "two", "isUp": True}, {"id": "old", "isUp": False}]

		self.skip_with(setup, nodes).assert_called_once_with(5)

	def test_dead_nodes_are_not_skipped_while_a_node_in_the_layout_is_down(self):
		setup = self.setup_with([("new", "storage-1"), ("two", "storage-2")], {"new"})
		nodes = [{"id": "new", "isUp": True}, {"id": "two", "isUp": False}, {"id": "old", "isUp": False}]

		self.skip_with(setup, nodes).assert_not_called()

	def test_nothing_is_skipped_once_garage_has_moved_the_data(self):
		setup = self.setup_with([("new", "storage-1")], {"new"})
		nodes = [{"id": "new", "isUp": True}, {"id": "old", "isUp": False}]

		self.skip_with(setup, nodes, is_draining=False).assert_not_called()

	def test_a_released_machine_is_taken_out_of_the_layout(self):
		setup = self.setup_with([("one", "storage-1"), ("two", "storage-2")], {"one", "two"})
		with patch.object(Setup, "apply_staged_layout") as apply:
			self.assertEqual(setup.remove_machines_from_layout(["storage-2"]), ["two"])

		self.assertEqual(self.removed(), ["two"])
		apply.assert_called_once()

	def test_garage_drains_while_an_older_version_is_draining(self):
		setup = self.setup_with([("one", "storage-1")], {"one"})
		versions = [{"version": 4, "status": "Current"}, {"version": 3, "status": "Draining"}]
		with patch.object(Setup, "layout_history", return_value={"versions": versions}):
			self.assertTrue(setup.is_draining())

		versions[1]["status"] = "Historical"
		with patch.object(Setup, "layout_history", return_value={"versions": versions}):
			self.assertFalse(setup.is_draining())

	def moving_blocks_with(self, setup: Setup, workers: dict[str, list[dict]]) -> list[str]:
		"""The nodes still moving blocks, with Garage reporting `workers` per answering node."""
		with patch.object(Setup, "list_workers", return_value={"success": workers, "error": {}}):
			return setup.get_nodes_moving_blocks()

	def test_nodes_with_blocks_queued_or_failed_are_still_moving_blocks(self):
		setup = self.setup_with(
			[("one", "storage-1"), ("two", "storage-2"), ("idle", "storage-2")], {"one", "two", "idle"}
		)
		workers = {
			"one": [{"name": "Block resync worker #1", "queueLength": 12, "persistentErrors": 0}],
			"two": [{"name": "Block resync worker #1", "queueLength": 0, "persistentErrors": 3}],
			"idle": [
				{"name": "Block resync worker #1", "queueLength": 0, "persistentErrors": 0},
				{"name": "Block scrub worker", "queueLength": 40},
			],
		}

		self.assertEqual(self.moving_blocks_with(setup, workers), ["one", "two"])

	def test_a_running_node_that_does_not_answer_is_still_moving_blocks(self):
		setup = self.setup_with([("one", "storage-1"), ("two", "storage-2")], {"one", "two"})
		workers = {"one": [{"name": "Block resync worker #1", "queueLength": 0, "persistentErrors": 0}]}

		self.assertEqual(self.moving_blocks_with(setup, workers), ["two"])

	def test_a_down_node_is_not_waited_on(self):
		setup = self.setup_with([("one", "storage-1"), ("dead", "storage-2")], {"one"})
		workers = {"one": [{"name": "Block resync worker #1", "queueLength": 0, "persistentErrors": 0}]}

		self.assertEqual(self.moving_blocks_with(setup, workers), [])


class UnitTestMetricsExporter(UnitTestCase):
	"""Each node gets its own machine token in its metadata, and a timer that trades it."""

	def test_the_node_keeps_its_role_and_gets_its_machine_token(self):
		cluster = SimpleNamespace(name="OSC-0001", region="blr", admin_port=3903, metrics_scrape_seconds=30)
		setup = Setup(cluster)
		node = machine(STORAGE)
		module = "cargo.object_storage.doctype.object_storage_cluster.setup"

		with (
			patch.object(Setup, "secrets", new_callable=PropertyMock, return_value={"metrics_token": "m"}),
			patch.object(Setup, "run") as run,
			patch(f"{module}.install_script", return_value="install agent"),
			patch(f"{module}.datum_refresh_script", return_value="install timer"),
			patch(f"{module}.generate_machine_token", return_value="machine-token") as generate,
			patch(f"{module}.AtlasClient.from_settings") as atlas,
		):
			setup.install_metrics_exporter(node)

		generate.assert_called_once_with(STORAGE, node["name"], "vm-storage")
		atlas.return_value.replace_vm_metadata.assert_called_once_with(
			"vm-storage", {"role": STORAGE, "datum_telemetry_token": "machine-token"}
		)
		self.assertEqual([call.args[1] for call in run.call_args_list], ["install timer", "install agent"])
