# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_datetime

from cargo.atlas_client import AtlasError, AtlasNotFound
from cargo.cargo.doctype.machine.machine import Machine, sync_all_machines
from cargo.client_models import NodeSpec
from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import ObjectStorageCluster
from cargo.testing import use_test_settings

MESH_ADDRESS = "fdaa:1:0:7::3"


def running_vm(mesh_ipv6: str | None = MESH_ADDRESS) -> dict:
	"""The parts of a `get_vm` reply Cargo reads, nested the way Atlas nests them."""
	return {
		"current_state": "running",
		"network": {"ipv4_internet_access": True, "mesh_ipv6": mesh_ipv6, "public_ipv4": None},
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
		status = self.machine.sync(self.client({"current_state": "failed", "error": "disk full"}))

		self.assertEqual(status, "Broken")
		self.assertIn("disk full", self.machine.error)

	def test_a_machine_atlas_no_longer_has_is_terminated(self):
		client = Mock()
		client.get_vm.side_effect = AtlasNotFound("gone")

		self.assertEqual(self.machine.sync(client), "Terminated")

	def test_requesting_records_the_id_and_waits_for_the_address(self):
		with patch("cargo.atlas_client.AtlasClient") as atlas:
			atlas.from_settings.return_value.create_vm.return_value = {"id": "vm-00003"}
			machine = Machine.request(
				self.cluster,
				NodeSpec(role="gateway", cpu_millicores=2000, ram_gb=4, disk_gb=20),
				base_image="img-1",
			)

		self.assertEqual(machine.status, "Pending")
		self.assertEqual(machine.vm_id, "vm-00003")
		self.assertIsNone(machine.address)
		self.assertEqual((machine.cpu_millicores, machine.ram_gb, machine.disk_size_gb), (2000, 4, 20))

	def test_a_protected_request_asks_atlas_to_protect_the_machine(self):
		with patch("cargo.atlas_client.AtlasClient") as atlas:
			atlas.from_settings.return_value.create_vm.return_value = {"id": "vm-00004"}
			machine = Machine.request(
				self.cluster,
				NodeSpec(role="storage", cpu_millicores=1000, ram_gb=2, disk_gb=20),
				base_image="img-1",
				is_termination_protected=True,
			)

		self.assertTrue(
			atlas.from_settings.return_value.create_vm.call_args.kwargs["is_termination_protected"]
		)
		self.assertTrue(machine.is_termination_protected)

	def test_the_protection_atlas_reports_is_recorded(self):
		self.machine.sync(self.client({**running_vm(), "is_termination_protected": True}))

		self.assertEqual(frappe.db.get_value("Machine", self.machine.name, "is_termination_protected"), 1)

	def test_protecting_asks_atlas_and_records_it(self):
		with patch("cargo.atlas_client.AtlasClient") as atlas:
			self.machine.set_termination_protection(True)

		atlas.from_settings.return_value.set_vm_termination_protection.assert_called_once_with(
			self.machine.vm_id, True
		)
		self.assertEqual(frappe.db.get_value("Machine", self.machine.name, "is_termination_protected"), 1)

	def test_protection_atlas_refuses_is_not_recorded(self):
		with patch("cargo.atlas_client.AtlasClient") as atlas, self.assertRaises(frappe.ValidationError):
			atlas.from_settings.return_value.set_vm_termination_protection.side_effect = AtlasError("no")
			self.machine.set_termination_protection(True)

		self.assertEqual(frappe.db.get_value("Machine", self.machine.name, "is_termination_protected"), 0)

	def test_a_protected_machine_keeps_its_protection_unless_released(self):
		self.machine.db_set("is_termination_protected", 1)
		with patch("cargo.atlas_client.AtlasClient") as atlas:
			self.machine.terminate()

		atlas.from_settings.return_value.set_vm_termination_protection.assert_not_called()
		atlas.from_settings.return_value.terminate_vm.assert_called_once_with(self.machine.vm_id)

	def test_releasing_a_protected_machine_lifts_protection_before_terminating(self):
		self.machine.db_set("is_termination_protected", 1)
		with patch("cargo.atlas_client.AtlasClient") as atlas:
			client = atlas.from_settings.return_value
			calls = Mock()
			calls.attach_mock(client.set_vm_termination_protection, "protect")
			calls.attach_mock(client.terminate_vm, "terminate")
			self.assertTrue(self.machine.terminate(release_protection=True))

		self.assertEqual([call[0] for call in calls.mock_calls], ["protect", "terminate"])
		client.set_vm_termination_protection.assert_called_once_with(self.machine.vm_id, False)
		self.assertEqual(self.machine.status, "Terminated")
		self.assertFalse(self.machine.is_termination_protected)

	def test_a_machine_is_photographed_as_a_cached_system_image(self):
		"""Both flags are what lets a host build a warm template from the image."""
		tags = {"purpose": "pilot", "pilot_version": "1.2.3"}
		with patch("cargo.atlas_client.AtlasClient") as atlas:
			atlas.from_settings.return_value.create_snapshot.return_value = "img-9"
			self.assertEqual(self.machine.snapshot("IMG-0001-version-16", tags), "img-9")

		atlas.from_settings.return_value.create_snapshot.assert_called_once_with(
			self.machine.vm_id,
			"IMG-0001-version-16",
			image_type="system",
			cache_image=True,
			memory_snapshot=True,
			tags=tags,
		)

	def test_the_public_address_is_never_taken_for_the_mesh_one(self):
		"""Cargo reaches machines over the mesh only; the public address is for the proxy."""
		self.machine.sync(self.client(running_vm()))

		self.assertEqual(self.machine.address, MESH_ADDRESS)

	def test_resizing_asks_atlas_and_waits_for_the_sync_to_record_the_size(self):
		with patch("cargo.atlas_client.AtlasClient") as atlas:
			atlas.from_settings.return_value.get_vm.return_value = {"current_state": "stopped"}
			self.machine.resize_machine(cpu_millicores=4000, ram_gb=8, disk_gb=40)

		atlas.from_settings.return_value.resize_vm.assert_called_once_with(
			self.machine.vm_id, cpu_millicores=4000, memory_mib=8192, disk_mib=40960
		)
		self.machine.reload()
		self.assertEqual(self.machine.disk_size_gb, 20)

	def test_a_smaller_disk_is_refused_before_asking_atlas(self):
		with patch("cargo.atlas_client.AtlasClient") as atlas:
			with self.assertRaises(frappe.ValidationError):
				self.machine.resize_machine(cpu_millicores=4000, ram_gb=8, disk_gb=10)

		atlas.from_settings.return_value.resize_vm.assert_not_called()

	def test_a_machine_that_is_not_stopped_is_not_resized(self):
		with patch("cargo.atlas_client.AtlasClient") as atlas:
			atlas.from_settings.return_value.get_vm.return_value = {"current_state": "running"}
			with self.assertRaises(frappe.ValidationError):
				self.machine.resize_machine(cpu_millicores=4000, ram_gb=8, disk_gb=40)

		atlas.from_settings.return_value.resize_vm.assert_not_called()

	def test_a_resize_atlas_refuses_leaves_the_shape_unchanged(self):
		with patch("cargo.atlas_client.AtlasClient") as atlas:
			atlas.from_settings.return_value.get_vm.return_value = {"current_state": "stopped"}
			atlas.from_settings.return_value.resize_vm.side_effect = AtlasError("out_of_capacity")
			with self.assertRaises(frappe.ValidationError):
				self.machine.resize_machine(cpu_millicores=4000, ram_gb=8, disk_gb=40)

		self.machine.reload()
		self.assertEqual(self.machine.disk_size_gb, 20)

	def test_a_power_action_is_sent_to_atlas(self):
		with patch("cargo.atlas_client.AtlasClient") as atlas:
			self.machine.change_power_state("stop")

		atlas.from_settings.return_value.set_power_state.assert_called_once_with(self.machine.vm_id, "stop")

	def test_an_unknown_power_action_is_refused(self):
		with patch("cargo.atlas_client.AtlasClient") as atlas:
			with self.assertRaises(frappe.ValidationError):
				self.machine.change_power_state("delete")

		atlas.from_settings.return_value.set_power_state.assert_not_called()

	def test_growing_the_disk_asks_atlas_and_waits_for_the_sync_to_record_the_size(self):
		with patch("cargo.atlas_client.AtlasClient") as atlas:
			self.machine.grow_disk(30)

		atlas.from_settings.return_value.grow_disk.assert_called_once_with(self.machine.vm_id, 30720)
		self.machine.reload()
		self.assertEqual(self.machine.disk_size_gb, 20)

	def test_the_size_atlas_applied_is_recorded(self):
		vm = {
			**running_vm(),
			"compute": {"cpu_millicores": 4000, "memory_mib": 8192},
			"disk": {"size_mib": 40960},
		}
		self.machine.sync(self.client(vm))

		self.machine.reload()
		self.assertEqual(
			(self.machine.cpu_millicores, self.machine.ram_gb, self.machine.disk_size_gb), (4000, 8, 40)
		)

	def test_a_disk_that_does_not_grow_is_refused(self):
		with patch("cargo.atlas_client.AtlasClient") as atlas:
			with self.assertRaises(frappe.ValidationError):
				self.machine.grow_disk(20)

		atlas.from_settings.return_value.grow_disk.assert_not_called()

	def test_a_stopped_machine_is_stopped_and_keeps_its_address(self):
		self.machine.sync(self.client(running_vm()))
		self.machine.sync(self.client({"current_state": "stopped"}))

		self.assertEqual(self.machine.status, "Stopped")
		self.assertEqual(self.machine.address, MESH_ADDRESS)

	def test_a_paused_machine_is_paused(self):
		self.machine.sync(self.client({"current_state": "paused"}))

		self.assertEqual(self.machine.status, "Paused")

	def test_an_unchanged_machine_is_not_saved_again(self):
		self.machine.sync(self.client(running_vm()))
		modified = self.machine.modified

		self.machine.sync(self.client(running_vm()))

		self.assertEqual(
			get_datetime(frappe.db.get_value("Machine", self.machine.name, "modified")),
			get_datetime(modified),
		)

	def test_every_machine_but_terminated_is_synced_and_changed_owners_told(self):
		self.machine.db_set("status", "Running")
		terminated = frappe.copy_doc(self.machine)
		terminated.status = "Terminated"
		terminated.vm_id = f"vm-{frappe.generate_hash(length=8)}"
		terminated.insert()

		with (
			patch("cargo.atlas_client.AtlasClient") as atlas,
			patch(f"{ObjectStorageCluster.__module__}.ObjectStorageCluster.sync_machines") as sync_machines,
		):
			atlas.from_settings.return_value.get_vm.return_value = {"current_state": "stopped"}
			sync_all_machines()

		synced = [call.args[0] for call in atlas.from_settings.return_value.get_vm.call_args_list]
		self.assertIn(self.machine.vm_id, synced)
		self.assertNotIn(terminated.vm_id, synced)
		self.assertEqual(frappe.db.get_value("Machine", self.machine.name, "status"), "Stopped")
		sync_machines.assert_called()
