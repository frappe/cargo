# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.synchronization import filelock

from cargo.atlas_client import AtlasNotFound
from cargo.cargo.doctype.machine.machine import Machine, sync_pending_machines
from cargo.client_models import NodeSpec
from cargo.testing import use_test_settings

MESH_ADDRESS = "fdaa:1:0:7::3"


def running_vm(mesh_ipv6: str | None = MESH_ADDRESS) -> dict:
	"""The parts of a `get_vm` reply Cargo reads, nested the way Atlas nests them."""
	return {
		"current_state": "running",
		"network": {"egress": "uplink", "mesh_ipv6": mesh_ipv6, "public_ipv4": "203.0.113.10"},
	}


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
		status = self.machine.sync(self.client(running_vm()))

		self.assertEqual(status, "Running")
		self.assertEqual(self.machine.address, MESH_ADDRESS)

	def test_a_machine_still_booting_has_no_address_yet(self):
		status = self.machine.sync(self.client({"current_state": "created"}))

		self.assertEqual(status, "Pending")
		self.assertIsNone(self.machine.address)

	def test_a_running_machine_atlas_gave_no_address_for_is_broken(self):
		status = self.machine.sync(self.client(running_vm(mesh_ipv6=None)))

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

	def test_the_public_address_is_never_taken_for_the_mesh_one(self):
		"""Cargo reaches machines over the mesh only; the public address is for the proxy."""
		self.machine.sync(self.client(running_vm()))

		self.assertEqual(self.machine.address, MESH_ADDRESS)


class IntegrationTestSyncNow(IntegrationTestCase):
	"""The Desk button: the same sync the sweep runs, on demand."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		self.cluster = frappe.get_doc({"doctype": "Object Storage Cluster"}).insert()

	def machine(self, **values) -> Machine:
		return frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": self.cluster.doctype,
				"reference_name": self.cluster.name,
				"role": "storage",
				"disk_size_gb": 20,
				"status": "Pending",
				**values,
			}
		).insert()

	def atlas_reporting(self, payload):
		atlas = patch("cargo.atlas_client.AtlasClient")
		client = atlas.start()
		self.addCleanup(atlas.stop)
		client.from_settings.return_value.get_vm.return_value = payload

		return client

	def test_a_machine_atlas_has_not_built_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.machine().sync_now()

	def test_it_takes_what_atlas_reports(self):
		self.atlas_reporting(running_vm())
		machine = self.machine(vm_id=f"vm-{frappe.generate_hash(length=8)}")

		self.assertEqual(machine.sync_now(), "Running")
		self.assertEqual(machine.address, MESH_ADDRESS)

	def test_the_owner_hears_when_the_state_moves(self):
		self.atlas_reporting(running_vm())
		machine = self.machine(vm_id=f"vm-{frappe.generate_hash(length=8)}")

		with patch("cargo.cargo.doctype.machine.machine.notify_owner") as notify:
			machine.sync_now()

		notify.assert_called_once_with(self.cluster.doctype, self.cluster.name)

	def test_the_owner_is_left_alone_when_nothing_moved(self):
		self.atlas_reporting({"current_state": "created"})
		machine = self.machine(vm_id=f"vm-{frappe.generate_hash(length=8)}")

		with patch("cargo.cargo.doctype.machine.machine.notify_owner") as notify:
			machine.sync_now()

		notify.assert_not_called()


class IntegrationTestConcurrentSync(IntegrationTestCase):
	"""The sweep and the Desk button can sync one machine at the same moment."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		cluster = frappe.get_doc({"doctype": "Object Storage Cluster"}).insert()
		self.owner = (cluster.doctype, cluster.name)
		self.name = (
			frappe.get_doc(
				{
					"doctype": "Machine",
					"reference_doctype": cluster.doctype,
					"reference_name": cluster.name,
					"role": "storage",
					"disk_size_gb": 20,
					"vm_id": f"vm-{frappe.generate_hash(length=8)}",
					"status": "Pending",
				}
			)
			.insert()
			.name
		)
		self.client = Mock()
		self.client.get_vm.return_value = running_vm()

	def copy(self) -> Machine:
		return frappe.get_doc("Machine", self.name)

	def test_a_copy_opened_before_the_other_synced_does_not_clash_with_it(self):
		"""Both loaded first, as a Desk form and the sweep would: the later save used to raise
		TimestampMismatchError."""
		button, sweep = self.copy(), self.copy()

		sweep.sync_exclusively(self.client)
		button.sync_exclusively(self.client)

		self.assertEqual(self.copy().status, "Running")

	def test_only_the_sync_that_saw_the_move_reports_it(self):
		button, sweep = self.copy(), self.copy()

		self.assertTrue(sweep.sync_exclusively(self.client))
		self.assertFalse(button.sync_exclusively(self.client))

	def test_the_owner_hears_of_one_move_once(self):
		button = self.copy()
		with patch("cargo.cargo.doctype.machine.machine.notify_owner") as notify:
			self.copy().sync_exclusively(self.client)
			button.sync_now()

		notify.assert_not_called()

	def test_the_sweep_skips_a_machine_being_synced_and_carries_on(self):
		other = frappe.copy_doc(self.copy())
		other.vm_id = f"vm-{frappe.generate_hash(length=8)}"
		other.insert()

		with (
			patch("cargo.cargo.doctype.machine.machine.SYNC_LOCK_TIMEOUT", 0.1),
			patch("cargo.atlas_client.AtlasClient.from_settings", return_value=self.client),
			patch("cargo.cargo.doctype.machine.machine.notify_owner"),
			filelock(f"machine-sync-{self.name}"),
		):
			sync_pending_machines()

		self.assertEqual(self.copy().status, "Pending")
		self.assertEqual(frappe.db.get_value("Machine", other.name, "status"), "Running")

	def test_the_button_says_so_when_the_sweep_holds_the_machine(self):
		with (
			patch("cargo.cargo.doctype.machine.machine.SYNC_LOCK_TIMEOUT", 0.1),
			patch("cargo.atlas_client.AtlasClient.from_settings", return_value=self.client),
			filelock(f"machine-sync-{self.name}"),
			self.assertRaisesRegex(frappe.ValidationError, "already being synced"),
		):
			self.copy().sync_now()
