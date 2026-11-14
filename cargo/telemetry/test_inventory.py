from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.telemetry.inventory import MACHINE, get_machine_samples


class IntegrationTestMachineInventory(IntegrationTestCase):
	"""The machines that Cargo tells Datum about each minute."""

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("machine_inventory_test")
		self.addCleanup(frappe.db.rollback, save_point="machine_inventory_test")
		self.enterContext(patch("cargo.telemetry.inventory.get_cargo_vm", return_value="cargo-vm"))

	def add_machine(self, vm_id: str, status: str, reference_doctype: str = "Object Storage Cluster"):
		# Only the row counts here, so the test skips the provisioning that a real machine runs on insert.
		frappe.get_doc(
			{
				"doctype": "Machine",
				"name": vm_id,
				"vm_id": vm_id,
				"role": "storage",
				"status": status,
				"reference_doctype": reference_doctype,
				"reference_name": "cluster-a",
			}
		).db_insert()

	def labels_of(self, machine: str) -> dict | None:
		samples = [sample for sample in get_machine_samples() if sample["labels"]["machine"] == machine]
		return samples[0]["labels"] if samples else None

	def test_a_running_machine_names_its_role_and_cluster(self):
		self.add_machine("vm-storage", "Running")

		self.assertEqual(
			self.labels_of("vm-storage"), {"machine": "vm-storage", "role": "storage", "cluster": "cluster-a"}
		)

	def test_a_machine_that_is_not_running_is_left_out(self):
		self.add_machine("vm-stopped", "Stopped")

		self.assertIsNone(self.labels_of("vm-stopped"))

	def test_a_machine_outside_object_storage_has_no_cluster(self):
		self.add_machine("vm-telemetry", "Running", reference_doctype="Datum Server")

		self.assertNotIn("cluster", self.labels_of("vm-telemetry"))

	def test_the_vm_of_cargo_is_listed_with_the_role_cargo(self):
		self.assertEqual(self.labels_of("cargo-vm"), {"machine": "cargo-vm", "role": "cargo"})
		self.assertTrue(all(sample["metric"] == MACHINE for sample in get_machine_samples()))
