from datetime import datetime
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.client_models import GATEWAY, STORAGE
from cargo.object_storage.client import Client
from cargo.object_storage.doctype.object_storage_cluster import test_object_storage_cluster
from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import ObjectStorageCluster
from cargo.object_storage.doctype.object_storage_cluster.setup import Setup
from cargo.object_storage.metadata_snapshot import is_snapshot_due, take_metadata_snapshots
from cargo.testing import use_test_settings

SNAPSHOT = "2026-10-23T10:00:00Z"
NOW = datetime(2026, 10, 23, 12, 0, 0)


class IntegrationTestMetadataSnapshot(IntegrationTestCase):
	"""Every node of a live cluster snapshots its metadata on the interval Cargo Settings sets."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		for name in frappe.get_all(
			"Object Storage Cluster", filters={"activated_on": ("is", "set")}, pluck="name"
		):
			frappe.db.set_value("Object Storage Cluster", name, {"status": "Draft", "activated_on": None})

		self.cluster = frappe.get_doc(
			{
				"doctype": "Object Storage Cluster",
				"rpc_secret": "rpc",
				"admin_token": "admin",
				"metrics_token": "metrics",
			}
		).insert()
		self.vm_ids: dict[str, str] = {}
		self.gateway = self.add_machine(GATEWAY)
		self.storage = [self.add_machine(STORAGE) for _ in range(3)]
		self.cluster.db_set({"status": "Active", "activated_on": frappe.utils.now_datetime()})
		self.cluster.reload()
		frappe.db.set_single_value("Cargo Settings", "metadata_snapshot_interval_hours", 6)

	add_machine = test_object_storage_cluster.IntegrationTestObjectStorageCluster.add_machine

	def last_snapshot(self):
		return frappe.db.get_value("Object Storage Cluster", self.cluster.name, "last_metadata_snapshot_at")

	def test_a_snapshot_is_due_once_the_interval_has_passed(self):
		self.assertTrue(is_snapshot_due(None, 6, NOW))
		self.assertTrue(is_snapshot_due(datetime(2026, 10, 23, 6, 0, 0), 6, NOW))
		self.assertFalse(is_snapshot_due(datetime(2026, 10, 23, 7, 0, 0), 6, NOW))
		self.assertTrue(is_snapshot_due(datetime(2026, 10, 23, 11, 30, 0), 0.5, NOW))

	def test_every_node_is_asked_at_once(self):
		with patch.object(Client, "call", return_value={"success": {}, "error": {}}) as call:
			Client(self.cluster).create_metadata_snapshot()

		call.assert_called_once_with("CreateMetadataSnapshot", "POST", params={"node": "*"})

	def test_a_due_cluster_is_snapshotted_and_stamped(self):
		with patch.object(
			Setup, "create_metadata_snapshot", return_value={"success": {}, "error": {}}
		) as create:
			take_metadata_snapshots()

		create.assert_called_once()
		self.assertIsNotNone(self.last_snapshot())

	def test_nothing_is_asked_before_the_interval_passes(self):
		self.cluster.db_set("last_metadata_snapshot_at", frappe.utils.now_datetime())

		with patch.object(Setup, "create_metadata_snapshot") as create:
			take_metadata_snapshots()

		create.assert_not_called()

	def test_a_zero_interval_stops_snapshots(self):
		frappe.db.set_single_value("Cargo Settings", "metadata_snapshot_interval_hours", 0)

		with patch.object(Setup, "create_metadata_snapshot") as create:
			take_metadata_snapshots()

		create.assert_not_called()

	def test_no_snapshot_while_a_workflow_owns_the_cluster(self):
		with (
			patch("cargo.object_storage.metadata_snapshot.get_running_workflow", return_value="WF-0001"),
			patch.object(Setup, "create_metadata_snapshot") as create,
		):
			take_metadata_snapshots()

		create.assert_not_called()

	def test_a_refusing_node_is_named_and_the_run_still_counts(self):
		refused = {"success": {"aaa": None}, "error": {"bbb": "Cannot acquire lock"}}
		with (
			patch.object(Setup, "create_metadata_snapshot", return_value=refused),
			patch.object(Setup, "get_layout_nodes", return_value=[("bbb", self.storage[1])]),
			patch("frappe.log_error") as log_error,
		):
			take_metadata_snapshots()

		self.assertIn(f"{self.storage[1]}: Cannot acquire lock", log_error.call_args.kwargs["message"])
		self.assertIsNotNone(self.last_snapshot())


class IntegrationTestMetadataRestore(IntegrationTestCase):
	"""A restore is typed in by hand and refused unless it can finish without losing quorum."""

	setUp = IntegrationTestMetadataSnapshot.setUp
	add_machine = test_object_storage_cluster.IntegrationTestObjectStorageCluster.add_machine

	def held(self, snapshots: list[str]):
		return patch.object(Setup, "get_metadata_snapshots", return_value=snapshots)

	def test_one_machine_is_restored_alone(self):
		with (
			self.held([SNAPSHOT]),
			patch.object(Setup, "healthy_nodes", return_value=set(self.storage)),
			patch.object(ObjectStorageCluster, "_restore_metadata") as restore,
		):
			self.cluster.restore_metadata_snapshot(SNAPSHOT, self.storage[0])

		restore.run_as_workflow.assert_called_once_with(SNAPSHOT, [self.storage[0]])

	def test_the_whole_cluster_is_every_machine(self):
		with self.held([SNAPSHOT]), patch.object(ObjectStorageCluster, "_restore_metadata") as restore:
			self.cluster.restore_metadata_snapshot(SNAPSHOT)

		snapshot, machines = restore.run_as_workflow.call_args.args
		self.assertEqual(snapshot, SNAPSHOT)
		self.assertEqual(set(machines), {self.gateway, *self.storage})

	def test_one_machine_waits_for_its_peers_to_serve(self):
		with (
			patch.object(Setup, "healthy_nodes", return_value={self.storage[0], self.storage[1]}),
			self.assertRaisesRegex(frappe.ValidationError, self.storage[2]),
		):
			self.cluster.restore_metadata_snapshot(SNAPSHOT, self.storage[0])

	def test_the_whole_cluster_needs_every_machine_running(self):
		frappe.db.set_value("Machine", self.storage[1], "status", "Stopped")

		with self.assertRaisesRegex(frappe.ValidationError, self.storage[1]):
			frappe.get_doc("Object Storage Cluster", self.cluster.name).restore_metadata_snapshot(SNAPSHOT)

	def test_only_a_name_the_node_lists_is_restored(self):
		for name in ("latest", f"{SNAPSHOT}/../..", "../db.lmdb", ""):
			with (
				self.subTest(name=name),
				self.held([SNAPSHOT]),
				patch.object(ObjectStorageCluster, "_restore_metadata") as restore,
				self.assertRaisesRegex(frappe.ValidationError, "not on every machine"),
			):
				self.cluster.restore_metadata_snapshot(name)

			restore.run_as_workflow.assert_not_called()

	def test_a_cluster_that_never_served_has_nothing_to_restore(self):
		self.cluster.db_set("activated_on", None)

		with self.assertRaisesRegex(frappe.ValidationError, "served"):
			self.cluster.restore_metadata_snapshot(SNAPSHOT)

	def test_a_machine_without_the_snapshot_refuses_the_restore(self):
		held = {self.storage[0]: [SNAPSHOT], self.storage[1]: ["2026-10-23T04:00:00Z"]}
		with (
			patch.object(
				Setup, "get_metadata_snapshots", side_effect=lambda row: held.get(row["name"], [SNAPSHOT])
			),
			patch.object(ObjectStorageCluster, "_restore_metadata") as restore,
			self.assertRaisesRegex(frappe.ValidationError, "2026-10-23T04:00:00Z"),
		):
			self.cluster.restore_metadata_snapshot(SNAPSHOT)

		restore.run_as_workflow.assert_not_called()

	def test_tables_are_not_repaired_before_every_node_reconnects(self):
		with (
			patch.object(
				Setup, "get_layout_nodes", return_value=[("aaa", self.storage[0]), ("bbb", self.storage[1])]
			),
			patch.object(Setup, "get_running_node_ids", return_value=["aaa"]),
			patch.object(ObjectStorageCluster, "defer_current_task") as defer,
			patch.object(Setup, "repair_tables") as repair,
		):
			self.cluster.repair_metadata_tables([self.storage[0]])

		defer.assert_called_once()
		repair.assert_not_called()

	def test_tables_are_repaired_on_each_restored_machine_once_all_are_back(self):
		with (
			patch.object(
				Setup, "get_layout_nodes", return_value=[("aaa", self.storage[0]), ("bbb", self.storage[1])]
			),
			patch.object(Setup, "get_running_node_ids", return_value=["aaa", "bbb"]),
			patch.object(Setup, "repair_tables") as repair,
		):
			self.cluster.repair_metadata_tables([self.storage[0], self.storage[1]])

		self.assertEqual([args.args[0]["name"] for args in repair.call_args_list], self.storage[:2])

	def test_the_restore_script_is_given_the_snapshot_and_the_directory(self):
		with patch.object(Setup, "run") as run:
			Setup(self.cluster).restore_metadata({"name": self.storage[0], "address": "fdaa::1"}, SNAPSHOT)

		sent = run.call_args.args[1]
		self.assertIn(f"export SNAPSHOT={SNAPSHOT}", sent)
		self.assertIn(f"export METADATA_DIR={self.cluster.metadata_dir}", sent)
		self.assertIn('install -m 600 "$SOURCE" "$LIVE/data.mdb"', sent)
