from unittest.mock import MagicMock, patch

import frappe
from botocore.exceptions import ClientError
from frappe.tests import IntegrationTestCase
from frappe.utils.password import set_encrypted_password

from cargo.client_models import GATEWAY, STORAGE
from cargo.object_storage.doctype.object_storage_cluster import test_object_storage_cluster
from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import ObjectStorageCluster
from cargo.object_storage.doctype.object_storage_cluster.setup import (
	BACKUP_SSH_TIMEOUT,
	BACKUP_TRANSFER_TIMEOUT,
	METADATA_SNAPSHOT_INTERVAL,
	Setup,
)
from cargo.object_storage.metadata_snapshot import upload_metadata_snapshot, upload_metadata_snapshots
from cargo.testing import use_test_settings

SNAPSHOT = "2026-10-23T10:00:00Z"


class IntegrationTestMetadataBackup(IntegrationTestCase):
	"""Garage snapshots every node on its own clock; Cargo ships each node's newest one offsite."""

	add_machine = test_object_storage_cluster.IntegrationTestObjectStorageCluster.add_machine

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
				"metadata_backup_endpoint": "https://s3.example.com/bucket",
				"metadata_bucket_access_key": "GK123",
			}
		).insert()
		self.vm_ids: dict[str, str] = {}
		self.gateway = self.add_machine(GATEWAY)
		self.storage = [self.add_machine(STORAGE) for _ in range(3)]
		self.cluster.db_set({"status": "Active", "activated_on": frappe.utils.now_datetime()})
		set_encrypted_password(
			self.cluster.doctype, self.cluster.name, "s3cr3t-value", "metadata_bucket_secret_key"
		)
		self.cluster.reload()

	def last_backup(self):
		return frappe.db.get_value("Object Storage Cluster", self.cluster.name, "last_metadata_snapshot_at")

	def test_each_machine_uploads_in_its_own_long_job(self):
		with patch("frappe.enqueue") as enqueue:
			upload_metadata_snapshots()

		self.assertEqual(
			{call.kwargs["machine_name"] for call in enqueue.call_args_list}, {self.gateway, *self.storage}
		)
		self.assertEqual({call.kwargs["queue"] for call in enqueue.call_args_list}, {"long"})
		self.assertTrue(all(call.kwargs["timeout"] > BACKUP_SSH_TIMEOUT for call in enqueue.call_args_list))
		self.assertTrue(all(call.kwargs["deduplicate"] for call in enqueue.call_args_list))

	def test_no_upload_without_a_backup_bucket(self):
		self.cluster.db_set("metadata_backup_endpoint", None)

		with patch("frappe.enqueue") as enqueue:
			upload_metadata_snapshots()

		enqueue.assert_not_called()

	def test_no_upload_while_a_workflow_owns_the_cluster(self):
		with (
			patch("cargo.object_storage.metadata_snapshot.get_running_workflow", return_value="WF-0001"),
			patch("frappe.enqueue") as enqueue,
		):
			upload_metadata_snapshots()

		enqueue.assert_not_called()

	def test_a_new_snapshot_is_uploaded_pruned_and_stamped(self):
		with (
			patch.object(Setup, "get_newest_metadata_snapshot", return_value=SNAPSHOT),
			patch.object(Setup, "has_metadata_backup_of", return_value=False),
			patch.object(Setup, "upload_metadata_snapshot") as upload,
			patch.object(Setup, "prune_metadata_backups") as prune,
		):
			upload_metadata_snapshot(self.cluster.name, self.storage[0])

		self.assertEqual(upload.call_args.args[1], SNAPSHOT)
		prune.assert_called_once()
		self.assertIsNotNone(self.last_backup())

	def test_a_snapshot_already_in_the_bucket_is_not_uploaded_again(self):
		with (
			patch.object(Setup, "get_newest_metadata_snapshot", return_value=SNAPSHOT),
			patch.object(Setup, "has_metadata_backup_of", return_value=True),
			patch.object(Setup, "upload_metadata_snapshot") as upload,
		):
			upload_metadata_snapshot(self.cluster.name, self.storage[0])

		upload.assert_not_called()
		self.assertIsNone(self.last_backup())

	def test_a_node_without_a_finished_snapshot_uploads_nothing(self):
		with (
			patch.object(Setup, "get_newest_metadata_snapshot", return_value=None),
			patch.object(Setup, "upload_metadata_snapshot") as upload,
		):
			upload_metadata_snapshot(self.cluster.name, self.storage[0])

		upload.assert_not_called()

	def test_a_failed_upload_prunes_nothing_and_is_logged(self):
		with (
			patch.object(Setup, "get_newest_metadata_snapshot", return_value=SNAPSHOT),
			patch.object(Setup, "has_metadata_backup_of", return_value=False),
			patch.object(Setup, "upload_metadata_snapshot", side_effect=RuntimeError),
			patch.object(Setup, "prune_metadata_backups") as prune,
			patch("frappe.log_error") as log_error,
		):
			upload_metadata_snapshot(self.cluster.name, self.storage[0])

		prune.assert_not_called()
		log_error.assert_called_once()
		self.assertIsNone(self.last_backup())

	def test_the_newest_snapshot_is_read_past_ssh_warnings(self):
		garage = Setup(self.cluster)
		output = "Warning: Permanently added 'fdaa::7' to the list of known hosts.\nsnapshot=2026-11-13T09:00:04Z\n"

		with patch.object(Setup, "run", return_value=output):
			self.assertEqual(
				garage.get_newest_metadata_snapshot(garage.machine(self.storage[0])), "2026-11-13T09:00:04Z"
			)

		with patch.object(Setup, "run", return_value="Warning: Permanently added 'fdaa::7'.\n"):
			self.assertIsNone(garage.get_newest_metadata_snapshot(garage.machine(self.storage[0])))

	def test_bucket_keys_go_through_machine_metadata(self):
		garage = Setup(self.cluster)
		machine = garage.machine(self.storage[0])

		with (
			patch.object(Setup, "replace_vm_metadata") as replace,
			patch.object(Setup, "run") as run,
		):
			garage.upload_metadata_snapshot(machine, SNAPSHOT)

		self.assertEqual(
			replace.call_args.args[1],
			{"metadata_bucket_access_key": "GK123", "metadata_bucket_secret_key": "s3cr3t-value"},
		)
		sent = run.call_args.args[1]
		self.assertNotIn("s3cr3t-value", sent)
		self.assertIn(f"export BACKUP_PREFIX={self.cluster.name}/{self.storage[0]}", sent)
		self.assertIn(f"export SNAPSHOT={SNAPSHOT}", sent)
		self.assertIn(f"export TRANSFER_TIMEOUT={BACKUP_TRANSFER_TIMEOUT}", sent)
		self.assertEqual(run.call_args.kwargs["timeout"], BACKUP_SSH_TIMEOUT)

	def test_garage_is_told_to_snapshot_on_its_own(self):
		garage = Setup(self.cluster)
		environment = garage.install_environment(garage.machine(self.storage[0]))

		self.assertEqual(environment["METADATA_SNAPSHOT_INTERVAL"], METADATA_SNAPSHOT_INTERVAL)


class IntegrationTestMetadataRestore(IntegrationTestCase):
	"""A restore is typed in by hand and refused unless it can finish without losing quorum."""

	add_machine = test_object_storage_cluster.IntegrationTestObjectStorageCluster.add_machine

	def setUp(self):
		IntegrationTestMetadataBackup.setUp(self)
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

	def test_a_cluster_that_never_served_has_nothing_to_restore(self):
		self.cluster.db_set("activated_on", None)

		with self.assertRaisesRegex(frappe.ValidationError, "served"):
			self.cluster.restore_metadata_snapshot(SNAPSHOT, self.storage[0])

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

	def test_a_machine_without_its_backup_refuses_the_restore(self):
		self.has_backup.return_value = False

		with (
			patch.object(Setup, "healthy_nodes", return_value=set(self.storage)),
			patch.object(ObjectStorageCluster, "_restore_metadata") as restore,
			self.assertRaisesRegex(frappe.ValidationError, self.storage[1]),
		):
			self.cluster.restore_metadata_snapshot(SNAPSHOT, self.storage[1])

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
			self.cluster._restore_metadata(SNAPSHOT, [self.storage[0]])

		self.assertEqual([name for name, *_ in steps.mock_calls], ["fetch", "stop", "swap", "repair"])

	def test_a_restore_needs_a_backup_bucket(self):
		self.cluster.db_set("metadata_backup_endpoint", None)

		with (
			patch.object(Setup, "healthy_nodes", return_value=set(self.storage)),
			patch.object(ObjectStorageCluster, "_restore_metadata") as restore,
			self.assertRaisesRegex(frappe.ValidationError, "Metadata Backup Endpoint"),
		):
			self.cluster.restore_metadata_snapshot(SNAPSHOT, self.storage[0])

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
