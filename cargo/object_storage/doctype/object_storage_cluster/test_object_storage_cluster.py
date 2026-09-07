# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.object_storage.client_models import GATEWAY, STORAGE

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
		"""Dropped from the cluster, kept as a record: the audit trail is the point."""
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
		"""Setup is still deciding what joined; terminating underneath it is unrecoverable."""
		self.cluster.db_set("status", "Setting Up")
		self.cluster.reload()

		with self.assertRaises(frappe.ValidationError):
			self.cluster.release_machines([self.storage])

		self.assertEqual(self.machine_names(), {self.gateway, self.storage})
