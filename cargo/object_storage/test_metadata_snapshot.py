from datetime import datetime
from unittest.mock import MagicMock, patch

import frappe
from botocore.exceptions import ClientError
from frappe.tests import IntegrationTestCase
from frappe.utils.password import set_encrypted_password

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

	def test_no_upload_without_a_backup_bucket(self):
		with (
			patch.object(Setup, "create_metadata_snapshot", return_value={"success": {}, "error": {}}),
			patch.object(Setup, "upload_metadata_snapshots") as upload,
		):
			take_metadata_snapshots()

		upload.assert_not_called()

	def test_every_machine_uploads_and_one_failure_does_not_stop_the_rest(self):
		self.cluster.db_set(
			{
				"metadata_backup_endpoint": "https://s3.example.com/garage-metadata",
				"metadata_bucket_access_key": "GK123",
			}
		)
		with (
			patch.object(Setup, "create_metadata_snapshot", return_value={"success": {}, "error": {}}),
			patch.object(
				Setup, "upload_metadata_snapshots", side_effect=[RuntimeError, None, None, None]
			) as upload,
			patch.object(Setup, "prune_metadata_backups"),
			patch("frappe.log_error") as log_error,
		):
			take_metadata_snapshots()

		self.assertEqual(upload.call_count, 4)
		self.assertEqual(len({call.args[1] for call in upload.call_args_list}), 1)
		log_error.assert_called_once()
		self.assertIsNotNone(self.last_snapshot())

	def test_bucket_keys_go_through_machine_metadata(self):
		self.cluster.db_set(
			{
				"metadata_backup_endpoint": "https://s3.example.com/garage-metadata",
				"metadata_bucket_access_key": "GK123",
			}
		)
		self.cluster.metadata_bucket_secret_key = "s3cr3t-value"
		self.cluster.save()
		garage = Setup(self.cluster)
		machine = garage.machine(self.storage[0])

		with (
			patch.object(Setup, "replace_vm_metadata") as replace,
			patch.object(Setup, "run") as run,
		):
			garage.upload_metadata_snapshots(machine, SNAPSHOT)

		self.assertEqual(
			replace.call_args.args[1],
			{"metadata_bucket_access_key": "GK123", "metadata_bucket_secret_key": "s3cr3t-value"},
		)
		sent = run.call_args.args[1]
		self.assertNotIn("s3cr3t-value", sent)
		self.assertIn(f"{self.cluster.name}/{self.storage[0]}", sent)
		self.assertIn(f"export NAME={SNAPSHOT}", sent)

	def test_a_refusing_node_does_not_upload_an_older_snapshot_under_this_name(self):
		self.cluster.db_set(
			{
				"metadata_backup_endpoint": "https://s3.example.com/bucket",
				"metadata_bucket_access_key": "GK123",
			}
		)
		refused = {"success": {}, "error": {"bbb": "Cannot acquire lock"}}
		with (
			patch.object(Setup, "create_metadata_snapshot", return_value=refused),
			patch.object(Setup, "get_layout_nodes", return_value=[("bbb", self.storage[1])]),
			patch.object(Setup, "upload_metadata_snapshots") as upload,
			patch.object(Setup, "prune_metadata_backups"),
			patch("frappe.log_error"),
		):
			take_metadata_snapshots()

		uploaded = {call.args[0]["name"] for call in upload.call_args_list}
		self.assertEqual(uploaded, {self.gateway, self.storage[0], self.storage[2]})

	def test_a_restore_fetches_only_the_machines_own_backup(self):
		self.cluster.db_set(
			{
				"metadata_backup_endpoint": "https://s3.example.com/bucket",
				"metadata_bucket_access_key": "GK123",
			}
		)
		set_encrypted_password(
			self.cluster.doctype, self.cluster.name, "s3cr3t-value", "metadata_bucket_secret_key"
		)
		self.cluster.reload()
		garage = Setup(self.cluster)

		with patch.object(Setup, "replace_vm_metadata"), patch.object(Setup, "run") as run:
			garage.fetch_metadata_snapshot(garage.machine(self.storage[0]), SNAPSHOT)

		sent = run.call_args.args[1]
		self.assertIn(f"export BACKUP_PREFIX={self.cluster.name}/{self.storage[0]}", sent)
		self.assertNotIn("s3cr3t-value", sent)

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

	add_machine = test_object_storage_cluster.IntegrationTestObjectStorageCluster.add_machine

	def setUp(self):
		IntegrationTestMetadataSnapshot.setUp(self)
		self.cluster.db_set(
			{
				"metadata_backup_endpoint": "https://s3.example.com/bucket",
				"metadata_bucket_access_key": "GK123",
			}
		)
		self.backed_up = patch.object(Setup, "has_metadata_backup_of", return_value=True)
		self.has_backup = self.backed_up.start()
		self.addCleanup(patch.stopall)

	def test_one_machine_is_restored_alone(self):
		with (
			patch.object(Setup, "healthy_nodes", return_value=set(self.storage)),
			patch.object(ObjectStorageCluster, "_restore_metadata") as restore,
		):
			self.cluster.restore_metadata_snapshot(SNAPSHOT, self.storage[0])

		restore.run_as_workflow.assert_called_once_with(SNAPSHOT, [self.storage[0]])

	def test_the_whole_cluster_is_every_machine(self):
		with patch.object(ObjectStorageCluster, "_restore_metadata") as restore:
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

	def test_a_gateway_is_restored_on_what_a_storage_node_sees(self):
		"""The gateway may be the node that is down, so it cannot vouch for its peers."""
		frappe.db.set_value("Machine", self.storage[0], "address", "10.0.0.2")
		asked = []

		def healthy_nodes(garage):
			asked.append(garage.url)
			return set(self.storage) if "10.0.0.2" in garage.url else set()

		with (
			patch.object(Setup, "healthy_nodes", autospec=True, side_effect=healthy_nodes),
			patch.object(ObjectStorageCluster, "_restore_metadata") as restore,
		):
			frappe.get_doc("Object Storage Cluster", self.cluster.name).restore_metadata_snapshot(
				SNAPSHOT, self.gateway
			)

		restore.run_as_workflow.assert_called_once_with(SNAPSHOT, [self.gateway])
		self.assertEqual(asked, ["http://10.0.0.2:3903"])

	def test_the_whole_cluster_needs_every_machine_running(self):
		frappe.db.set_value("Machine", self.storage[1], "status", "Stopped")

		with self.assertRaisesRegex(frappe.ValidationError, self.storage[1]):
			frappe.get_doc("Object Storage Cluster", self.cluster.name).restore_metadata_snapshot(SNAPSHOT)

	def test_a_cluster_that_never_served_has_nothing_to_restore(self):
		self.cluster.db_set("activated_on", None)

		with self.assertRaisesRegex(frappe.ValidationError, "served"):
			self.cluster.restore_metadata_snapshot(SNAPSHOT)

	def test_only_the_two_newest_backups_are_kept(self):
		garage = Setup(self.cluster)
		machine = garage.machine(self.storage[0])
		prefix = f"{self.cluster.name}/{self.storage[0]}"
		names = ["2026-10-23T04:00:00Z", "2026-10-23T10:00:00Z", "2026-10-22T22:00:00Z", SNAPSHOT]
		page = {"Contents": [{"Key": f"{prefix}/{name}/db.lmdb"} for name in names]}

		with patch("cargo.object_storage.doctype.object_storage_cluster.setup.boto3.client") as client:
			set_encrypted_password(
				self.cluster.doctype, self.cluster.name, "s3cr3t-value", "metadata_bucket_secret_key"
			)
			client.return_value.get_paginator.return_value.paginate.return_value = [page]
			pruned = garage.prune_metadata_backups(machine)

		self.assertEqual(
			pruned, [f"{prefix}/2026-10-22T22:00:00Z/db.lmdb", f"{prefix}/2026-10-23T04:00:00Z/db.lmdb"]
		)
		deleted = [call.kwargs["Key"] for call in client.return_value.delete_object.call_args_list]
		self.assertEqual(deleted, pruned)

	def test_a_failed_upload_prunes_nothing(self):
		with (
			patch.object(Setup, "create_metadata_snapshot", return_value={"success": {}, "error": {}}),
			patch.object(Setup, "upload_metadata_snapshots", side_effect=RuntimeError),
			patch.object(Setup, "prune_metadata_backups") as prune,
			patch("frappe.log_error"),
		):
			take_metadata_snapshots()

		prune.assert_not_called()

	def test_a_machine_without_its_backup_refuses_the_restore(self):
		self.has_backup.side_effect = lambda machine, _: machine["name"] != self.storage[1]

		with (
			patch.object(ObjectStorageCluster, "_restore_metadata") as restore,
			self.assertRaisesRegex(frappe.ValidationError, self.storage[1]),
		):
			self.cluster.restore_metadata_snapshot(SNAPSHOT)

		restore.run_as_workflow.assert_not_called()

	def test_the_bucket_is_asked_for_the_machines_own_backup(self):
		set_encrypted_password(
			self.cluster.doctype, self.cluster.name, "s3cr3t-value", "metadata_bucket_secret_key"
		)
		self.cluster.reload()
		garage = Setup(self.cluster)
		machine = garage.machine(self.storage[0])
		missing = ClientError({"Error": {"Code": "404"}}, "HeadObject")
		self.backed_up.stop()

		with patch("cargo.object_storage.doctype.object_storage_cluster.setup.boto3.client") as client:
			self.assertTrue(garage.has_metadata_backup_of(machine, SNAPSHOT))
			client.return_value.head_object.side_effect = missing
			self.assertFalse(garage.has_metadata_backup_of(machine, SNAPSHOT))

		client.return_value.head_object.assert_called_with(
			Bucket="bucket", Key=f"{self.cluster.name}/{self.storage[0]}/{SNAPSHOT}/db.lmdb"
		)
		self.assertEqual(client.call_args.kwargs["endpoint_url"], "https://s3.example.com")

	def test_backups_are_fetched_before_any_machine_stops(self):
		steps = MagicMock()
		with (
			patch.object(ObjectStorageCluster, "fetch_metadata_snapshots", steps.fetch),
			patch.object(ObjectStorageCluster, "stop_garage_on_machines", steps.stop),
			patch.object(ObjectStorageCluster, "swap_in_metadata_snapshot", steps.swap),
			patch.object(ObjectStorageCluster, "repair_metadata_tables", steps.repair),
		):
			self.cluster._restore_metadata(SNAPSHOT, self.storage)

		self.assertEqual([name for name, *_ in steps.mock_calls], ["fetch", "stop", "swap", "repair"])

	def test_a_restore_needs_a_backup_bucket(self):
		self.cluster.db_set("metadata_backup_endpoint", None)

		with (
			patch.object(ObjectStorageCluster, "_restore_metadata") as restore,
			self.assertRaisesRegex(frappe.ValidationError, "Metadata Backup Endpoint"),
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
