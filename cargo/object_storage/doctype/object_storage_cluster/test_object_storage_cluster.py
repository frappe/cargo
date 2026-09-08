# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from contextlib import contextmanager
from typing import ClassVar
from unittest.mock import MagicMock, PropertyMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.object_storage.client_models import GATEWAY, STORAGE
from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
	ObjectStorageCluster,
)
from cargo.object_storage.garage import Garage

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class IntegrationTestObjectStorageCluster(IntegrationTestCase):
	"""Releasing machines: a failed run leaves them alone, so the operator does it by hand."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.cluster = frappe.get_doc({"doctype": "Object Storage Cluster"}).insert()
		# vm_id is unique across the table, so it cannot be a fixed string per role.
		self.vm_ids: dict[str, str] = {}
		self.gateway = self.add_machine(GATEWAY)
		self.storage = self.add_machine(STORAGE)

	def add_machine(self, role: str) -> str:
		vm_id = f"vm-{role}-{frappe.generate_hash(length=8)}"
		machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": self.cluster.doctype,
				"reference_name": self.cluster.name,
				"role": role,
				"disk_size_gb": 20,
				"vm_id": vm_id,
				"ipv4_address": "10.0.0.1",
				"status": "Running",
			}
		).insert()
		self.vm_ids[machine.name] = vm_id
		self.cluster.append("machines", {"machine": machine.name, "role": role})
		self.cluster.save()

		return machine.name

	def machine_names(self) -> set[str]:
		return {row.machine for row in frappe.get_doc("Object Storage Cluster", self.cluster.name).machines}

	def test_releasing_terminates_only_the_named_machines(self):
		with patch("cargo.object_storage.machines.AtlasClient") as atlas:
			self.cluster.release_machines([self.storage])

		atlas.from_settings.return_value.terminate_vm.assert_called_once_with(self.vm_ids[self.storage])
		self.assertEqual(self.machine_names(), {self.gateway})
		self.assertEqual(frappe.db.get_value("Machine", self.storage, "status"), "Terminated")

	def test_the_machine_row_outlives_the_release(self):
		with patch("cargo.object_storage.machines.AtlasClient"):
			self.cluster.release_machines([self.storage])

		self.assertTrue(frappe.db.exists("Machine", self.storage))

	def test_a_machine_of_another_cluster_is_refused(self):
		with patch("cargo.object_storage.machines.AtlasClient") as atlas:
			with self.assertRaises(frappe.ValidationError):
				self.cluster.release_machines([self.storage, "not-ours"])

		atlas.from_settings.assert_not_called()
		self.assertEqual(self.machine_names(), {self.gateway, self.storage})

	def test_releasing_nothing_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.cluster.release_machines([])

	def test_a_cluster_being_set_up_cannot_be_released(self):
		self.cluster.db_set("status", "Setting Up")
		self.cluster.reload()

		with self.assertRaises(frappe.ValidationError):
			self.cluster.release_machines([self.storage])

		self.assertEqual(self.machine_names(), {self.gateway, self.storage})


class IntegrationTestClusterReadiness(IntegrationTestCase):
	"""What Central is told, and when. Joining is not serving: the layout has to land first."""

	def setUp(self):
		frappe.set_user("Administrator")
		# Garage refuses a cluster with no secrets.
		self.cluster = frappe.get_doc(
			{
				"doctype": "Object Storage Cluster",
				"rpc_secret": "rpc",
				"admin_token": "admin",
				"metrics_token": "metrics",
			}
		).insert()
		self.cluster.db_set("status", "Active")
		self.cluster.reload()

	@contextmanager
	def _garage(self, layout_version: int):
		endpoints = {
			"base_url": "http://10.0.0.5:3903",
			"s3_endpoint": "http://10.0.0.5:3900",
			"web_endpoint": "http://10.0.0.5:3902",
		}
		with (
			patch.object(Garage, "layout_version", return_value=layout_version),
			# Applying the layout also makes the metadata bucket; that is its own test.
			patch.object(ObjectStorageCluster, "create_metadata_bucket_if_needed"),
			patch.object(
				ObjectStorageCluster, "central_endpoints", new_callable=PropertyMock, return_value=endpoints
			),
			patch(
				"cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster.CentralClient"
			) as central,
		):
			yield central.from_settings.return_value.register_storage_cluster

	def test_joined_but_unapplied_is_not_handed_out(self):
		with self._garage(layout_version=0) as register:
			self.cluster.inform_central_of_cluster_health("Active")

		self.assertFalse(register.call_args.kwargs["active"])
		self.assertNotIn("s3_endpoint", register.call_args.kwargs)

	def test_an_applied_layout_makes_it_servable(self):
		with self._garage(layout_version=3) as register:
			self.cluster.inform_central_of_cluster_health("Active")

		self.assertTrue(register.call_args.kwargs["active"])
		self.assertEqual(register.call_args.kwargs["s3_endpoint"], "http://10.0.0.5:3900")

	def test_applying_the_layout_tells_central(self):
		with self._garage(layout_version=1) as register, patch.object(Garage, "apply_layout"):
			self.cluster.apply_layout()

		self.assertTrue(register.call_args.kwargs["active"])

	def test_applying_the_layout_on_a_failed_cluster_says_nothing(self):
		self.cluster.db_set("status", "Failed")
		self.cluster.reload()

		with self._garage(layout_version=1) as register, patch.object(Garage, "apply_layout"):
			self.cluster.apply_layout()

		register.assert_not_called()

	def test_a_failed_cluster_is_withdrawn_without_asking_garage(self):
		with self._garage(layout_version=9) as register:
			self.cluster.inform_central_of_cluster_health("Failed")

		self.assertFalse(register.call_args.kwargs["active"])
		self.assertNotIn("base_url", register.call_args.kwargs)

	def test_an_unreachable_garage_reads_as_not_serving(self):
		"""layout_version() swallows GarageError and returns 0."""
		with self._garage(layout_version=0) as register:
			self.cluster.inform_central_of_cluster_health("Active")

		self.assertFalse(register.call_args.kwargs["active"])


class IntegrationTestLiveClusterRelease(IntegrationTestCase):
	"""A cluster that has served holds data, so releasing a node can cost a copy."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.cluster = frappe.get_doc(
			{
				"doctype": "Object Storage Cluster",
				"replication_factor": 2,
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

	add_machine = IntegrationTestObjectStorageCluster.add_machine

	@contextmanager
	def _joined(self, names: list[str]):
		with patch.object(Garage, "healthy_nodes", return_value=set(names)):
			yield

	def test_the_gateway_cannot_be_released(self):
		with self._joined(self.storage), self.assertRaises(frappe.ValidationError):
			self.cluster.release_machines([self.gateway])

	def test_a_serving_node_cannot_be_released(self):
		"""Releasing aids setup. A node that joined is working, not broken."""
		with self._joined(self.storage), self.assertRaises(frappe.ValidationError):
			self.cluster.release_machines([self.storage[0]])

		self.assertEqual(frappe.db.get_value("Machine", self.storage[0], "status"), "Running")

	def test_one_serving_node_refuses_the_whole_request(self):
		with self._joined(self.storage[:1]), self.assertRaises(frappe.ValidationError):
			self.cluster.release_machines([self.storage[0], self.storage[1]])

		self.assertEqual(frappe.db.get_value("Machine", self.storage[1], "status"), "Running")

	def test_a_node_that_never_joined_can_be_released(self):
		with (
			self._joined(self.storage[:2]),
			patch("cargo.object_storage.machines.AtlasClient"),
		):
			self.cluster.release_machines([self.storage[2]])

		self.assertEqual(frappe.db.get_value("Machine", self.storage[2], "status"), "Terminated")

	def test_a_silent_garage_refuses_the_whole_thing(self):
		"""healthy_nodes() returns an empty set on GarageError, which must not read as
		"nothing is serving" on a cluster that has served."""
		with self._joined([]), self.assertRaises(frappe.ValidationError):
			self.cluster.release_machines([self.storage[0]])

	def test_a_cluster_that_never_served_may_release_a_broken_node(self):
		"""The setup case: Garage is unreachable because the run failed, and the machines
		it left behind are the whole reason this exists."""
		self.cluster.db_set("activated_on", None)
		self.cluster.reload()

		with self._joined([]), patch("cargo.object_storage.machines.AtlasClient"):
			self.cluster.release_machines([self.storage[0]])

		self.assertEqual(frappe.db.get_value("Machine", self.storage[0], "status"), "Terminated")


class IntegrationTestMetadataBucket(IntegrationTestCase):
	"""Cargo's own bucket on the cluster, made when the cluster can hold one."""

	BUCKET: ClassVar[dict] = {"name": "osc-metadata", "access_key": "GK-access", "secret_key": "shh"}

	def setUp(self):
		frappe.set_user("Administrator")
		self.cluster = frappe.get_doc(
			{
				"doctype": "Object Storage Cluster",
				"rpc_secret": "rpc",
				"admin_token": "admin",
				"metrics_token": "metrics",
			}
		).insert()

	@contextmanager
	def _garage(self, bucket_exists: bool = False):
		"""What Garage answers for GetBucketInfo, and a stub for making the bucket.

		`admin` is patched rather than the client's method: building the real one needs a
		booted gateway to address."""
		admin = MagicMock()
		admin.bucket.return_value = {"id": "b1"} if bucket_exists else None
		with (
			patch.object(Garage, "admin", new_callable=PropertyMock, return_value=admin),
			patch.object(Garage, "create_metadata_bucket", return_value=dict(self.BUCKET)) as create,
		):
			create.admin = admin
			yield create

	def _record_bucket(self) -> None:
		self.cluster.db_set(
			{"metadata_bucket": self.BUCKET["name"], "metadata_bucket_access_key": self.BUCKET["access_key"]}
		)
		self.cluster.reload()

	@contextmanager
	def _applying(self):
		with (
			patch.object(Garage, "apply_layout"),
			patch.object(ObjectStorageCluster, "inform_central_of_cluster_health"),
		):
			yield

	def test_applying_the_layout_creates_the_bucket_and_keeps_its_secret(self):
		"""Applying is when the cluster can hold an object, so it is when the bucket is made."""
		self.cluster.db_set("status", "Active")
		self.cluster.reload()

		with self._garage(bucket_exists=False), self._applying():
			self.cluster.apply_layout()

		cluster = frappe.get_doc("Object Storage Cluster", self.cluster.name)
		self.assertEqual(cluster.metadata_bucket, self.BUCKET["name"])
		self.assertEqual(cluster.metadata_bucket_access_key, self.BUCKET["access_key"])
		self.assertEqual(cluster.get_password("metadata_bucket_secret_key"), self.BUCKET["secret_key"])

	def test_a_bucket_garage_still_has_is_left_alone(self):
		self._record_bucket()

		with self._garage(bucket_exists=True) as create:
			self.cluster.create_metadata_bucket_if_needed()

		create.assert_not_called()

	def test_a_bucket_garage_no_longer_has_is_made_again(self):
		"""The row can outlive the bucket -- dropped at Garage, or the cluster rebuilt
		under it. Uploads would fail on a name Cargo still believes in."""
		self._record_bucket()

		with self._garage(bucket_exists=False) as create:
			self.cluster.create_metadata_bucket_if_needed()

		create.assert_called_once()

	def test_garage_is_not_asked_before_anything_was_recorded(self):
		"""Nothing to check against, and the answer would not change the outcome."""
		with self._garage(bucket_exists=False) as create:
			self.cluster.create_metadata_bucket_if_needed()

		create.admin.bucket.assert_not_called()
		create.assert_called_once()

	def test_a_cluster_that_is_not_active_gets_no_bucket(self):
		"""A layout can be applied to a cluster that failed; it still serves nothing."""
		self.cluster.db_set("status", "Failed")
		self.cluster.reload()

		with self._garage() as create, self._applying():
			self.cluster.apply_layout()

		create.assert_not_called()
		self.assertIsNone(frappe.db.get_value("Object Storage Cluster", self.cluster.name, "metadata_bucket"))

	def test_activating_alone_makes_no_bucket(self):
		"""Nodes joining is not enough: without a layout they carry no storage role."""
		with self._garage() as create, patch.object(ObjectStorageCluster, "inform_central_of_cluster_health"):
			self.cluster.mark_cluster_status("Active", None)

		create.assert_not_called()


class IntegrationTestClusterPeers(IntegrationTestCase):
	"""Recording peers: one read of Garage, and only the machines that joined are written to."""

	def setUp(self):
		frappe.set_user("Administrator")
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
		self.storage = self.add_machine(STORAGE)

	add_machine = IntegrationTestObjectStorageCluster.add_machine

	def _admin(self, joined: list[str]) -> MagicMock:
		"""Garage answering that `joined` are up, each tagged with its machine name."""
		admin = MagicMock()
		admin.status.return_value = {
			"nodes": [
				{"id": f"node{index}", "addr": "10.0.0.1:3901", "isUp": True, "role": {"tags": [name]}}
				for index, name in enumerate(joined)
			]
		}
		admin.layout.return_value = {"stagedRoleChanges": []}

		return admin

	@contextmanager
	def _garage(self, joined: list[str]):
		admin = self._admin(joined)
		with (
			patch.object(Garage, "admin", new_callable=PropertyMock, return_value=admin),
			patch.object(Garage, "record_peers") as record,
		):
			yield admin, record

	def test_peers_are_read_once_and_written_to_joined_machines(self):
		with self._garage([self.gateway, self.storage]) as (admin, record):
			self.cluster.record_cluster_peers()

		self.assertEqual(admin.status.call_count, 1)
		self.assertEqual(admin.layout.call_count, 1)
		self.assertEqual(
			{call.args[0]["name"] for call in record.call_args_list}, {self.gateway, self.storage}
		)

	def test_a_machine_that_never_joined_is_not_written_to(self):
		with self._garage([self.gateway]) as (_, record):
			self.cluster.record_cluster_peers()

		self.assertEqual({call.args[0]["name"] for call in record.call_args_list}, {self.gateway})

	def test_nothing_is_written_when_garage_reports_no_peers(self):
		with self._garage([]) as (_, record):
			self.cluster.record_cluster_peers()

		record.assert_not_called()
