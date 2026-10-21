import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from cargo.client_models import GATEWAY, STORAGE
from cargo.object_storage.automation.disk_full import GrowStorageNodeDisks, predict_linear
from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import ObjectStorageCluster
from cargo.testing import use_test_settings

HOUR = 3600
GIB = 1024**3


class UnitTestPredictLinear(UnitTestCase):
	def test_steady_growth_is_carried_forward(self):
		samples = [(0, 10, 100), (HOUR, 20, 100), (2 * HOUR, 30, 100)]

		self.assertAlmostEqual(predict_linear(samples, 6 * HOUR), 90)

	def test_flat_usage_stays_flat(self):
		samples = [(0, 50, 100), (HOUR, 50, 100)]

		self.assertAlmostEqual(predict_linear(samples, 6 * HOUR), 50)

	def test_one_sample_is_its_own_prediction(self):
		self.assertEqual(predict_linear([(0, 42, 100)], 6 * HOUR), 42)


class GrowStorageDisksTestCase(IntegrationTestCase):
	"""A cluster with a gateway and two 100 GB storage nodes."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		frappe.db.set_single_value("Cargo Settings", "max_storage_node_disk", 500)
		frappe.db.set_single_value("Cargo Settings", "storage_disk_predict_hours", 6)
		frappe.db.set_single_value("Cargo Settings", "first_storage_disk_growth_percent", 50)
		frappe.db.set_single_value("Object Storage Health Settings", "disk_degraded_percent", 20)
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")
		frappe.clear_document_cache("Object Storage Health Settings", "Object Storage Health Settings")
		self.cluster = frappe.get_doc({"doctype": "Object Storage Cluster"}).insert()
		self.gateway = self.add_machine(GATEWAY, 20)
		self.storage = [self.add_machine(STORAGE, 100), self.add_machine(STORAGE, 100)]
		self.grower = GrowStorageNodeDisks(self.cluster)

	def add_machine(self, role: str, disk_gb: int) -> str:
		machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": self.cluster.doctype,
				"reference_name": self.cluster.name,
				"role": role,
				"disk_size_gb": disk_gb,
				"vm_id": f"vm-{role}-{frappe.generate_hash(length=8)}",
				"status": "Running",
			}
		).insert()
		self.cluster.append("machines", {"machine": machine.name, "role": role})
		self.cluster.save()

		return machine.name

	def record_extention(self, machine: str, before: int, after: int) -> None:
		frappe.get_doc(
			{
				"doctype": "Version",
				"ref_doctype": "Machine",
				"docname": machine,
				"data": json.dumps({"changed": [["disk_size_gb", before, after]]}),
			}
		).insert(ignore_permissions=True)

	def no_prediction(self, shortfall: int = 0):
		return patch.object(GrowStorageNodeDisks, "get_predicted_shortfall_gb", return_value=shortfall)


class IntegrationTestGrowStorageNodeDisks(GrowStorageDisksTestCase):
	def test_extentions_are_read_from_the_version_history(self):
		self.record_extention(self.storage[0], 50, 100)

		extentions = self.grower.get_last_extentions()

		self.assertEqual([(e.machine, e.size_gb) for e in extentions], [(self.storage[0], 50)])

	def test_the_gateway_disk_is_not_a_storage_extention(self):
		self.record_extention(self.gateway, 10, 20)

		self.assertEqual(self.grower.get_last_extentions(), [])

	def test_extend_by_is_used_as_given(self):
		with self.no_prediction(shortfall=300):
			self.assertEqual(self.grower.get_extention_size(last_extention_size=50, extend_by=30), 30)

	def test_the_last_extention_is_doubled(self):
		with self.no_prediction():
			self.assertEqual(self.grower.get_extention_size(last_extention_size=40), 80)

	def test_the_first_extention_grows_the_disk_by_half(self):
		with self.no_prediction():
			self.assertEqual(self.grower.get_extention_size(), 50)

	def test_a_larger_predicted_shortfall_wins(self):
		with self.no_prediction(shortfall=120):
			self.assertEqual(self.grower.get_extention_size(last_extention_size=40), 120)

	def test_no_disk_grows_past_the_maximum(self):
		with self.no_prediction():
			self.assertEqual(self.grower.get_extention_size(extend_by=1000), 400)

	def test_a_disk_at_the_maximum_points_at_adding_a_node(self):
		frappe.db.set_single_value("Cargo Settings", "max_storage_node_disk", 100)
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")
		with self.no_prediction(), self.assertRaisesRegex(frappe.ValidationError, "storage node"):
			self.grower.get_extention_size(extend_by=10)

	def test_no_maximum_is_refused(self):
		frappe.db.set_single_value("Cargo Settings", "max_storage_node_disk", 0)
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")
		with self.no_prediction(), self.assertRaises(frappe.ValidationError):
			self.grower.get_extention_size(extend_by=10)

	def test_the_shortfall_keeps_the_degraded_free_space_six_hours_out(self):
		"""90 GiB disk, 10 GiB more used every hour: 120 GiB in 6h, which needs 150 GiB at 20% free."""
		start = datetime(2026, 10, 21, 10)
		lines = []
		for hour, used in enumerate((40, 50, 60)):
			node = {
				"role": {"tags": [self.storage[0]]},
				"dataPartition": {"total": 90 * GIB, "available": (90 - used) * GIB},
			}
			lines.append(
				json.dumps(
					{
						"timestamp": (start + timedelta(hours=hour)).isoformat(),
						"cluster": self.cluster.name,
						"nodes": [node],
					}
				)
			)

		with tempfile.TemporaryDirectory() as directory:
			log = Path(directory) / "cluster_health.json.log"
			log.write_text("\n".join(lines) + "\n")
			with patch("cargo.object_storage.automation.disk_full.history_file", return_value=log):
				self.assertEqual(self.grower.get_predicted_shortfall_gb(), 60)

	def test_disks_grow_in_lockstep_from_the_largest(self):
		frappe.db.set_value("Machine", self.storage[1], "disk_size_gb", 120)
		self.cluster.db_set("status", "Active")
		with patch.object(ObjectStorageCluster, "_grow_storage_disks") as grow:
			self.grower.grow_disks(30)

		grow.run_as_workflow.assert_called_once_with(150)

	def test_nothing_to_grow_points_at_adding_a_node(self):
		self.cluster.db_set("status", "Active")
		with self.assertRaisesRegex(frappe.ValidationError, "storage node"):
			self.grower.grow_disks(0)

	def test_a_cluster_that_is_not_active_is_not_grown(self):
		with self.assertRaises(frappe.ValidationError):
			self.grower.grow_disks(10)


class IntegrationTestGrowStorageDisksFlow(GrowStorageDisksTestCase):
	"""The workflow `grow_disks` starts: Atlas, then the filesystems, then Garage's layout."""

	def cluster_doc(self) -> ObjectStorageCluster:
		return frappe.get_doc("Object Storage Cluster", self.cluster.name)

	def test_atlas_is_asked_only_for_disks_still_short(self):
		frappe.db.set_value("Machine", self.storage[1], "disk_size_gb", 150)
		with patch("cargo.atlas_client.AtlasClient") as atlas:
			self.cluster_doc().request_disk_growth(150)

		vm_id = frappe.db.get_value("Machine", self.storage[0], "vm_id")
		atlas.from_settings.return_value.grow_disk.assert_called_once_with(vm_id, 150 * 1024)

	def test_the_flow_waits_until_atlas_reports_the_new_size(self):
		reported = {"current_state": "running", "network": {"mesh_ipv6": "fdaa::1"}}
		with (
			patch("cargo.atlas_client.AtlasClient") as atlas,
			patch.object(ObjectStorageCluster, "defer_current_task") as defer,
		):
			atlas.from_settings.return_value.get_vm.return_value = {
				**reported,
				"disk": {"size_mib": 100 * 1024},
			}
			self.cluster_doc().wait_for_disk_growth(150)
			defer.assert_called_once()

			defer.reset_mock()
			atlas.from_settings.return_value.get_vm.return_value = {
				**reported,
				"disk": {"size_mib": 150 * 1024},
			}
			self.cluster_doc().wait_for_disk_growth(150)
			defer.assert_not_called()

		self.assertEqual(frappe.db.get_value("Machine", self.storage[0], "disk_size_gb"), 150)

	def test_only_storage_filesystems_are_grown(self):
		with patch("cargo.object_storage.doctype.object_storage_cluster.setup.run_over_ssh") as ssh:
			self.cluster_doc().grow_storage_filesystems()

		self.assertEqual(ssh.call_count, 2)
		self.assertIn(f"export DATA_DIR={self.cluster.data_dir}", ssh.call_args.args[1])
		self.assertIn("resize2fs", ssh.call_args.args[1])

	def test_only_a_changed_capacity_is_staged_and_applied(self):
		frappe.db.set_value("Machine", self.storage[0], "disk_size_gb", 150)
		roles = [
			{"id": "n1", "zone": "z", "tags": [self.storage[0]], "capacity": 100 * 1000**3},
			{"id": "n2", "zone": "z", "tags": [self.storage[1]], "capacity": 100 * 1000**3},
			{"id": "n0", "zone": "z", "tags": [self.gateway], "capacity": None},
		]
		with (
			patch("cargo.object_storage.client.Client.layout", return_value={"version": 4, "roles": roles}),
			patch("cargo.object_storage.client.Client.assign_roles") as assign,
			patch("cargo.object_storage.client.Client.apply_layout") as apply,
		):
			self.cluster_doc().apply_storage_capacities()

		assign.assert_called_once_with(
			[{"id": "n1", "zone": "z", "tags": [self.storage[0]], "capacity": 150 * 1000**3}]
		)
		apply.assert_called_once_with(5)
