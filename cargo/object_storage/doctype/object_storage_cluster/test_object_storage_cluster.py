# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.object_storage.client_models import GATEWAY, STORAGE
from cargo.object_storage.health import ClusterHealth

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]

ATLAS = "cargo.atlas_client.AtlasClient.create_vms"


class IntegrationTestObjectStorageCluster(IntegrationTestCase):
	"""
	Integration tests for ObjectStorageCluster.
	Use this class for testing interactions between multiple components.
	"""

	def cluster(self, replication_factor: int = 2):
		cluster = frappe.new_doc("Object Storage Cluster")
		cluster.replication_factor = replication_factor

		return cluster.insert()

	def add(self, cluster, role: str, disk_gb: int, vm_id: str = "vm-1"):
		with patch(ATLAS, return_value=[vm_id]):
			if role == GATEWAY:
				return cluster.add_gateway_node(cpu=2, ram_gb=4, disk_gb=disk_gb)

			return cluster.add_storage_node(cpu=2, ram_gb=4, disk_gb=disk_gb)

	def test_machines_carry_their_own_disks(self):
		cluster = self.cluster()
		for index, disk_gb in enumerate((100, 500)):
			self.add(cluster, STORAGE, disk_gb, vm_id=f"vm-{index}")

		self.assertEqual(cluster.status, "Pending")
		self.assertEqual(len(cluster.storage_nodes), 2)
		self.assertEqual(
			frappe.get_all(
				"Machine",
				filters={"name": ("in", cluster.associated_machines)},
				pluck="disk_size_gb",
				order_by="creation",
			),
			[100, 500],
		)

	def test_a_cluster_takes_one_gateway(self):
		cluster = self.cluster()
		self.add(cluster, GATEWAY, 20)

		self.assertEqual(cluster.gateway_node, cluster.machines[0].machine)
		self.assertRaises(frappe.ValidationError, self.add, cluster, GATEWAY, 20)

	def test_a_machine_atlas_would_not_build_is_forgotten(self):
		cluster = self.cluster()
		with patch(ATLAS, side_effect=RuntimeError("no capacity")):
			self.assertRaises(frappe.ValidationError, cluster.add_storage_node, 2, 4, 100)

		cluster.reload()
		self.assertEqual(cluster.machines, [])
		self.assertEqual(cluster.status, "Draft")

	def test_setup_needs_a_cluster_that_can_serve(self):
		cluster = self.cluster()
		self.add(cluster, STORAGE, 100)

		# No gateway, and one storage node short of the replication factor.
		self.assertEqual(
			sorted(finding.reason for finding in ClusterHealth(cluster).blockers()),
			[
				"1 storage nodes running, 2 needed for a full copy",
				"this cluster has no gateway machine",
			],
		)
		self.assertRaises(frappe.ValidationError, cluster.setup_cluster)
