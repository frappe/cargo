# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.atlas_client import AtlasNotFound
from cargo.cargo.doctype.machine.machine import Machine
from cargo.client_models import NodeSpec
from cargo.testing import use_test_settings

MESH_ADDRESS = "fdaa:1:0:7::3"


class IntegrationTestMachine(IntegrationTestCase):
	"""How a Machine takes what Atlas reports about it."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		self.cluster = frappe.get_doc({"doctype": "Object Storage Cluster"}).insert()
		self.machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": self.cluster.doctype,
				"reference_name": self.cluster.name,
				"role": "storage",
				"disk_size_gb": 20,
				"vm_id": f"vm-{frappe.generate_hash(length=8)}",
				"status": "Pending",
			}
		).insert()

	def client(self, payload) -> Mock:
		client = Mock()
		client.get_vm.return_value = payload

		return client

	def test_a_running_machine_takes_the_address_atlas_reports(self):
		status = self.machine.sync(
			self.client({"current_state": "running", "wireguard_mesh_ipv6": MESH_ADDRESS})
		)

		self.assertEqual(status, "Running")
		self.assertEqual(self.machine.address, MESH_ADDRESS)

	def test_a_machine_still_booting_has_no_address_yet(self):
		status = self.machine.sync(self.client({"current_state": "created"}))

		self.assertEqual(status, "Pending")
		self.assertIsNone(self.machine.address)

	def test_a_running_machine_atlas_gave_no_address_for_is_broken(self):
		status = self.machine.sync(self.client({"current_state": "running"}))

		self.assertEqual(status, "Broken")
		self.assertIn("mesh address", self.machine.error)

	def test_a_machine_atlas_reports_failed_is_broken(self):
		status = self.machine.sync(self.client({"current_state": "failed"}))

		self.assertEqual(status, "Broken")

	def test_a_machine_atlas_no_longer_has_is_terminated(self):
		client = Mock()
		client.get_vm.side_effect = AtlasNotFound("gone")

		self.assertEqual(self.machine.sync(client), "Terminated")

	def test_requesting_records_the_id_and_waits_for_the_address(self):
		with patch("cargo.atlas_client.AtlasClient") as atlas:
			atlas.from_settings.return_value.create_vm.return_value = {"id": "vm-00003"}
			machine = Machine.request(
				self.cluster, NodeSpec(role="gateway", cpu=2, ram_gb=4, disk_gb=20), base_image="img-1"
			)

		self.assertEqual(machine.status, "Pending")
		self.assertEqual(machine.vm_id, "vm-00003")
		self.assertIsNone(machine.address)
