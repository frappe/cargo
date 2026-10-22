import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from cargo.client_models import GATEWAY, STORAGE
from cargo.object_storage.automation.disk_full import (
	DiskAtMaxCapacityError,
	Extention,
	GrowStorageNodeDisks,
	Sample,
	add_storage_node,
	predict_linear,
)
from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import ObjectStorageCluster
from cargo.object_storage.health.live import increase_disk_size_if_required
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
		frappe.db.set_single_value("Cargo Settings", "storage_disk_min_hours_between_growth", 6)
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

	def record_extention(self, machine: str, before: int, after: int, hours_ago: int = 0) -> None:
		"""Versions store changed values as text."""
		version = frappe.get_doc(
			{
				"doctype": "Version",
				"ref_doctype": "Machine",
				"docname": machine,
				"data": json.dumps({"changed": [["disk_size_gb", str(before), str(after)]]}),
			}
		).insert(ignore_permissions=True)
		creation = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-hours_ago)
		frappe.db.set_value("Version", version.name, "creation", creation, update_modified=False)

	def record_growth(self, before: int, after: int, hours_ago: int = 0) -> None:
		"""Disks grow in lockstep, so every storage machine gets the same version."""
		for machine in self.storage:
			self.record_extention(machine, before, after, hours_ago)

	def set_disks(self, disk_gb: int) -> None:
		for machine in self.storage:
			frappe.db.set_value("Machine", machine, "disk_size_gb", disk_gb)

	def write_log(self, entries: list[dict]) -> Path:
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		log = Path(directory.name) / "cluster_health.json.log"
		log.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
		patcher = patch("cargo.object_storage.automation.disk_full.history_file", return_value=log)
		patcher.start()
		self.addCleanup(patcher.stop)

		return log

	def usage(self, used_gb: int, total_gb: int = 100) -> Sample:
		return Sample(
			timestamp=datetime(2026, 10, 22, 10), used_bytes=used_gb * GIB, total_bytes=total_gb * GIB
		)


def storage_node(machine: str, used_gb: int, total_gb: int = 100) -> dict:
	return {
		"role": {"tags": [machine]},
		"dataPartition": {"total": total_gb * GIB, "available": (total_gb - used_gb) * GIB},
	}


class IntegrationTestLastExtention(GrowStorageDisksTestCase):
	def test_the_latest_growth_is_read_from_the_version_history(self):
		self.record_growth(50, 75, hours_ago=5)
		self.record_growth(75, 100, hours_ago=1)

		last = self.grower.get_last_extention()

		self.assertEqual((last.old, last.new), (75, 100))

	def test_setting_the_first_size_is_not_a_growth(self):
		self.record_growth(0, 100)

		self.assertIsNone(self.grower.get_last_extention())

	def test_no_history_is_no_extention(self):
		self.assertIsNone(self.grower.get_last_extention())

	def test_the_smallest_disk_is_the_one_read(self):
		self.record_extention(self.storage[0], 50, 100)
		frappe.db.set_value("Machine", self.storage[1], "disk_size_gb", 80)

		self.assertIsNone(self.grower.get_last_extention())


class IntegrationTestSamples(GrowStorageDisksTestCase):
	def test_no_log_is_no_samples(self):
		with patch(
			"cargo.object_storage.automation.disk_full.history_file", return_value=Path("/nonexistent/log")
		):
			self.assertEqual(self.grower.get_samples(), [])

	def test_the_fullest_storage_node_is_picked(self):
		self.write_log(
			[
				{
					"timestamp": "2026-10-22T10:00:00",
					"cluster": self.cluster.name,
					"nodes": [
						storage_node(self.gateway, 90),
						storage_node(self.storage[0], 20),
						storage_node(self.storage[1], 40),
					],
				},
				{
					"timestamp": "2026-10-22T10:01:00",
					"cluster": "other",
					"nodes": [storage_node(self.storage[0], 99)],
				},
				{
					"timestamp": "2026-10-22T10:02:00",
					"cluster": self.cluster.name,
					"nodes": [storage_node(self.storage[0], 30), storage_node(self.storage[1], 41)],
				},
			]
		)

		samples = self.grower.get_samples()

		self.assertEqual([sample.used_bytes // GIB for sample in samples], [40, 41])


class IntegrationTestRequiredDiskSize(GrowStorageDisksTestCase):
	def required(self, used_gb: int = 0) -> int:
		with patch.object(GrowStorageNodeDisks, "get_samples", return_value=[self.usage(used_gb)]):
			return self.grower.get_required_disk_size()

	def test_the_first_growth_adds_half(self):
		self.assertEqual(self.required(), 150)

	def test_the_last_growth_is_doubled(self):
		self.record_growth(50, 100)

		self.assertEqual(self.required(), 200)

	def test_a_larger_prediction_wins(self):
		"""200 GiB used needs 250 GiB to keep 20% free."""
		self.assertEqual(self.required(used_gb=200), 250)

	def test_the_size_never_passes_the_maximum(self):
		self.assertEqual(self.required(used_gb=1000), 500)

	def test_a_disk_at_the_maximum_stays_at_the_maximum(self):
		self.set_disks(500)

		self.assertEqual(self.required(), 500)


class IntegrationTestGrow(GrowStorageDisksTestCase):
	def grown_to(self) -> int | None:
		with (
			patch.object(GrowStorageNodeDisks, "get_samples", return_value=[self.usage(0)]),
			patch.object(ObjectStorageCluster, "_grow_storage_disks") as grow,
		):
			self.grower.grow()

		return grow.run_as_workflow.call_args.args[0] if grow.run_as_workflow.called else None

	def test_the_first_growth_starts_the_workflow(self):
		self.assertEqual(self.grown_to(), 150)

	def test_an_older_growth_lets_the_next_through(self):
		self.record_growth(75, 100, hours_ago=7)

		self.assertEqual(self.grown_to(), 150)

	def test_a_recent_growth_holds_off_the_next(self):
		self.record_growth(75, 100, hours_ago=2)

		with self.assertRaisesRegex(frappe.ValidationError, "grown recently"):
			self.grown_to()

	def test_a_disk_grown_to_the_maximum_points_at_adding_a_node(self):
		self.set_disks(500)
		self.record_growth(400, 500, hours_ago=7)

		with self.assertRaises(DiskAtMaxCapacityError):
			self.grown_to()

	def test_no_maximum_is_refused_before_anything_grows(self):
		frappe.db.set_single_value("Cargo Settings", "max_storage_node_disk", 0)
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

		with self.assertRaisesRegex(frappe.ValidationError, "Set Max Storage Node Disk") as raised:
			self.grown_to()

		self.assertNotIsInstance(raised.exception, DiskAtMaxCapacityError)

	def test_a_disk_created_at_the_maximum_points_at_adding_a_node(self):
		self.set_disks(500)

		with self.assertRaises(DiskAtMaxCapacityError):
			self.grown_to()

	def running_workflow(self):
		return patch(
			"cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster.get_running_workflow",
			return_value="workflow-1",
		)

	def test_a_running_workflow_holds_off_the_growth(self):
		with self.running_workflow(), self.assertRaisesRegex(frappe.ValidationError, "still running"):
			self.grown_to()

	def test_a_running_workflow_holds_off_adding_a_node(self):
		self.set_disks(500)

		with (
			self.running_workflow(),
			self.assertRaisesRegex(frappe.ValidationError, "still running") as raised,
		):
			self.grown_to()

		self.assertNotIsInstance(raised.exception, DiskAtMaxCapacityError)


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


class IntegrationTestIncreaseDiskSizeIfRequired(GrowStorageDisksTestCase):
	"""The scheduled job: grows when the fullest node is under the degraded free space."""

	def setUp(self):
		super().setUp()
		frappe.db.set_value("Object Storage Cluster", {"status": "Active"}, "status", "Failed")
		self.cluster.db_set("status", "Active")
		self.setup = None

	def run_job(
		self, samples: list[Sample], grow_error: Exception | None = None, unjoined: list[str] | None = None
	):
		with (
			patch.object(GrowStorageNodeDisks, "get_samples", return_value=samples),
			patch.object(GrowStorageNodeDisks, "grow", side_effect=grow_error) as grow,
			patch("cargo.object_storage.automation.disk_full.add_storage_node") as add_node,
			patch.object(ObjectStorageCluster, "discover_machines_to_setup", return_value=unjoined or []),
			patch.object(ObjectStorageCluster, "setup") as setup,
		):
			increase_disk_size_if_required()

		self.setup = setup
		return grow, add_node

	def test_a_running_workflow_does_nothing(self):
		with patch(
			"cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster.get_running_workflow",
			return_value="workflow-1",
		):
			grow, add_node = self.run_job([self.usage(used_gb=85)])

		grow.assert_not_called()
		add_node.assert_not_called()

	def test_a_booting_storage_node_is_waited_for(self):
		frappe.db.set_value("Machine", self.storage[0], "status", "Pending")

		grow, _ = self.run_job([self.usage(used_gb=85)], unjoined=[self.storage[0]])

		grow.assert_not_called()
		self.setup.assert_not_called()

	def test_a_booted_node_that_has_not_joined_is_set_up(self):
		grow, _ = self.run_job([self.usage(used_gb=85)], unjoined=[self.storage[0]])

		self.setup.assert_called_once()
		grow.assert_not_called()

	def test_a_stopped_node_does_not_trigger_setup(self):
		frappe.db.set_value("Machine", self.storage[0], "status", "Stopped")

		grow, _ = self.run_job([self.usage(used_gb=85)], unjoined=[self.storage[0]])

		self.setup.assert_not_called()
		grow.assert_called_once()

	def test_no_active_cluster_does_nothing(self):
		frappe.db.set_value("Object Storage Cluster", {"status": "Active"}, "status", "Failed")
		grow, add_node = self.run_job([self.usage(used_gb=85)])

		grow.assert_not_called()
		add_node.assert_not_called()

	def test_no_samples_yet_does_nothing(self):
		grow, add_node = self.run_job([])

		grow.assert_not_called()
		add_node.assert_not_called()

	def test_a_disk_with_room_is_left_alone(self):
		grow, _ = self.run_job([self.usage(used_gb=50)])

		grow.assert_not_called()

	def test_a_disk_under_the_degraded_free_space_is_grown(self):
		grow, _ = self.run_job([self.usage(used_gb=85)])

		grow.assert_called_once()

	def test_a_disk_at_the_maximum_adds_a_node(self):
		_, add_node = self.run_job([self.usage(used_gb=85)], grow_error=DiskAtMaxCapacityError("at max"))

		add_node.assert_called_once()


class IntegrationTestAddStorageNode(GrowStorageDisksTestCase):
	def test_a_new_node_is_left_to_boot_before_setup(self):
		with (
			patch.object(ObjectStorageCluster, "add_storage_node") as add,
			patch.object(ObjectStorageCluster, "setup") as setup,
		):
			add_storage_node(frappe.get_doc("Object Storage Cluster", self.cluster.name))

		add.assert_called_once()
		setup.assert_not_called()
