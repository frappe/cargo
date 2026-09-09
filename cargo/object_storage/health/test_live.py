# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.garage_admin_client import GarageError
from cargo.object_storage.client_models import GATEWAY, STORAGE
from cargo.object_storage.health import live as live_module
from cargo.object_storage.health.live import (
	CRITICAL,
	DEGRADED,
	HEALTHY,
	UNKNOWN,
	LiveHealth,
	history_file,
	prune_history,
)

GB = 1024**3
SETTINGS = "Object Storage Health Settings"


def node(machine: str, up: bool = True, seen: int = 0, free: float = 0.9) -> dict:
	total = 64 * GB

	return {
		"id": f"garage-{machine}",
		"isUp": up,
		"lastSeenSecsAgo": seen,
		"role": {"tags": [machine], "capacity": 10 * GB},
		"dataPartition": {"available": int(total * free), "total": total},
		"metadataPartition": {"available": int(total * free), "total": total},
	}


def cluster_health(**overrides) -> dict:
	return {
		"status": "healthy",
		"partitions": 256,
		"partitionsQuorum": 256,
		"partitionsAllOk": 256,
		**overrides,
	}


class IntegrationTestLiveHealth(IntegrationTestCase):
	"""The verdict a cluster gets from one read of its gateway, and the log it leaves behind."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.cluster = frappe.get_doc({"doctype": "Object Storage Cluster"}).insert()
		self.cluster.db_set("activated_on", frappe.utils.now_datetime())
		self.cluster.reload()
		self.machine = self.add_machine(STORAGE)
		history_file().unlink(missing_ok=True)

	def tearDown(self):
		history_file().unlink(missing_ok=True)

	def add_machine(self, role: str) -> str:
		machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": self.cluster.doctype,
				"reference_name": self.cluster.name,
				"role": role,
				"disk_size_gb": 20,
				"vm_id": f"vm-{frappe.generate_hash(length=8)}",
				"ipv4_address": "10.0.0.1",
				"status": "Running",
			}
		).insert()
		self.cluster.append("machines", {"machine": machine.name, "role": role})
		self.cluster.save()

		return machine.name

	def health(self, health: dict | None = None, nodes: list[dict] | None = None, error: str = ""):
		"""A LiveHealth whose gateway says what the test wants."""
		patcher = patch.object(live_module, "GarageAdminClient")
		client = patcher.start()
		self.addCleanup(patcher.stop)

		admin = client.for_cluster.return_value
		if error:
			admin.health.side_effect = GarageError(error)
		else:
			admin.health.return_value = health if health is not None else cluster_health()
			admin.status.return_value = {"nodes": nodes if nodes is not None else [node(self.machine)]}

		return LiveHealth(self.cluster)

	def test_a_cluster_with_nothing_wrong_is_healthy(self):
		self.assertEqual(self.health().check().severity, HEALTHY)

	def test_an_unreachable_gateway_is_critical_at_once(self):
		"""No grace period: the gateway not answering is the loudest thing that can happen."""
		live = self.health(error="connection refused")
		finding = live.record()

		self.assertEqual(finding.severity, CRITICAL)
		self.assertIn("could not be reached", finding.reason)
		self.assertEqual(frappe.db.get_value("Object Storage Cluster", self.cluster.name, "health"), CRITICAL)

	def test_an_unreachable_gateway_suppresses_every_other_finding(self):
		"""The rest would be invented from data we never received."""
		self.assertEqual(len(self.health(error="connection refused").findings()), 1)

	def test_partitions_that_cannot_be_written_are_critical(self):
		finding = self.health(health=cluster_health(partitionsQuorum=200)).check()

		self.assertEqual(finding.severity, CRITICAL)
		self.assertIn("56 of 256", finding.reason)

	def test_partitions_missing_a_copy_are_degraded(self):
		finding = self.health(health=cluster_health(partitionsAllOk=250)).check()

		self.assertEqual(finding.severity, DEGRADED)
		self.assertIn("missing a copy", finding.reason)

	def test_a_gateway_reporting_no_partitions_says_nothing_about_quorum(self):
		self.assertEqual(self.health(health={"status": "healthy"}).check().severity, HEALTHY)

	def test_a_node_down_past_the_threshold_is_degraded(self):
		finding = self.health(nodes=[node(self.machine, up=False, seen=340)]).check()

		self.assertEqual(finding.severity, DEGRADED)
		self.assertIn(self.machine, finding.reason)
		self.assertIn("340s", finding.reason)

	def test_a_node_that_only_blipped_is_not_held_against_the_cluster(self):
		self.assertEqual(self.health(nodes=[node(self.machine, up=False, seen=5)]).check().severity, HEALTHY)

	def test_a_full_disk_is_critical_and_a_filling_one_degraded(self):
		self.assertEqual(self.health(nodes=[node(self.machine, free=0.08)]).check().severity, CRITICAL)
		self.assertEqual(self.health(nodes=[node(self.machine, free=0.15)]).check().severity, DEGRADED)

	def test_a_node_missing_from_the_layout_is_skipped(self):
		"""No role means it has not joined yet, so it is not late -- it is not expected."""
		self.assertEqual(
			self.health(nodes=[{"id": "x", "isUp": True, "role": None}]).check().severity, HEALTHY
		)

	def test_the_worst_finding_wins(self):
		nodes = [node("full-one", free=0.05), node("filling-one", free=0.15)]

		self.assertEqual(self.health(nodes=nodes).check().severity, CRITICAL)

	def test_a_cluster_still_being_built_is_not_judged(self):
		self.cluster.db_set("activated_on", None)
		self.cluster.reload()
		live = self.health(error="connection refused")

		self.assertEqual(live.check().severity, UNKNOWN)

	def test_a_failed_build_is_critical_without_asking_the_gateway(self):
		self.cluster.db_set("status", "Failed")
		self.cluster.reload()
		live = self.health()

		self.assertEqual(live.check().severity, CRITICAL)
		live.admin.health.assert_not_called()

	def test_an_unchanged_verdict_is_not_rewritten(self):
		self.health().record()
		self.cluster.reload()
		with patch.object(type(self.cluster), "db_set") as written:
			self.health().record()

		written.assert_not_called()


class IntegrationTestHealthHistory(IntegrationTestCase):
	"""The local copy kept for when datum was unreachable."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.cluster = frappe.get_doc({"doctype": "Object Storage Cluster"}).insert()
		self.cluster.db_set("activated_on", frappe.utils.now_datetime())
		self.cluster.reload()
		self.path = history_file()
		self.path.unlink(missing_ok=True)

	def tearDown(self):
		self.path.unlink(missing_ok=True)
		self.path.with_suffix(".tmp").unlink(missing_ok=True)

	def record(self, error: str = "") -> None:
		with patch.object(live_module, "GarageAdminClient") as client:
			admin = client.for_cluster.return_value
			if error:
				admin.health.side_effect = GarageError(error)
			else:
				admin.health.return_value = cluster_health()
				admin.status.return_value = {"nodes": [node("OSC-storage-0001")]}
			LiveHealth(self.cluster).record()

	def lines(self) -> list[dict]:
		return [json.loads(line) for line in self.path.read_text().splitlines()]

	def age(self, index: int, hours: int) -> None:
		"""Backdate one line, so a test does not have to wait six hours."""
		entries = self.lines()
		entries[index]["timestamp"] = frappe.utils.add_to_date(
			frappe.utils.now_datetime(), hours=-hours
		).isoformat()
		self.path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")

	def test_every_run_appends_a_line(self):
		for _ in range(3):
			self.record()

		self.assertEqual(len(self.lines()), 3)

	def test_a_line_carries_the_numbers_behind_the_verdict(self):
		self.record()
		entry = self.lines()[0]

		self.assertEqual(entry["cluster"], self.cluster.name)
		self.assertEqual(entry["severity"], HEALTHY)
		self.assertEqual(entry["health"], cluster_health())
		self.assertEqual(entry["nodes"][0]["role"]["tags"], ["OSC-storage-0001"])

	def test_an_unreachable_gateway_is_still_written_down(self):
		"""This is the case the log exists for."""
		self.record(error="connection refused")
		entry = self.lines()[0]

		self.assertEqual(entry["severity"], CRITICAL)
		self.assertEqual(entry["error"], "connection refused")

	def test_lines_are_appended_in_order(self):
		for _ in range(3):
			self.record()
		stamps = [entry["timestamp"] for entry in self.lines()]

		self.assertEqual(stamps, sorted(stamps))

	def test_pruning_drops_what_is_past_the_window(self):
		self.record()
		self.record()
		self.age(0, hours=frappe.get_cached_doc(SETTINGS).history_hours + 1)

		prune_history()

		self.assertEqual(len(self.lines()), 1)

	def test_pruning_keeps_what_is_still_inside_the_window(self):
		self.record()
		self.age(0, hours=frappe.get_cached_doc(SETTINGS).history_hours - 1)
		before = self.path.read_text()

		prune_history()

		self.assertEqual(self.path.read_text(), before)

	def test_pruning_everything_leaves_a_file_the_next_run_can_append_to(self):
		self.record()
		self.age(0, hours=frappe.get_cached_doc(SETTINGS).history_hours + 5)

		prune_history()
		self.assertEqual(self.path.read_text(), "")

		self.record()
		self.assertEqual(len(self.lines()), 1)

	def test_pruning_a_log_that_does_not_exist_is_a_no_op(self):
		self.path.unlink(missing_ok=True)

		prune_history()

		self.assertFalse(self.path.exists())

	def test_pruning_leaves_no_scratch_file_behind(self):
		self.record()
		prune_history()

		self.assertFalse(self.path.with_suffix(".tmp").exists())

	def test_a_log_that_cannot_be_written_does_not_cost_the_verdict(self):
		"""The cluster's health matters more than the copy of it."""
		with patch.object(live_module.Path, "open", side_effect=OSError("read-only")):
			self.record()

		self.assertEqual(frappe.db.get_value("Object Storage Cluster", self.cluster.name, "health"), HEALTHY)


class IntegrationTestHealthSettings(IntegrationTestCase):
	"""Thresholds decide the scheduled verdict, so a broken one is refused at the form."""

	def setUp(self):
		frappe.set_user("Administrator")

	def saved(self, **changes) -> bool:
		settings = frappe.get_single(SETTINGS)
		settings.update(
			{
				"disk_degraded_percent": 20,
				"disk_critical_percent": 10,
				"node_offline_seconds": 120,
				"admin_timeout_seconds": 5,
				"history_hours": 6,
				**changes,
			}
		)
		try:
			settings.save()
			return True
		except frappe.ValidationError:
			return False
		finally:
			frappe.db.rollback()

	def test_sane_thresholds_are_accepted(self):
		self.assertTrue(self.saved())
		self.assertTrue(self.saved(disk_critical_percent=1, disk_degraded_percent=100))

	def test_a_percentage_over_one_hundred_is_refused(self):
		"""Every disk would be over the line, so every cluster would read critical."""
		self.assertFalse(self.saved(disk_critical_percent=101, disk_degraded_percent=102))

	def test_a_percentage_of_zero_is_refused(self):
		"""No disk is under zero percent free, so a full one would read healthy."""
		self.assertFalse(self.saved(disk_critical_percent=0))

	def test_critical_must_be_lower_than_degraded(self):
		self.assertFalse(self.saved(disk_critical_percent=25))

	def test_a_duration_of_zero_is_refused(self):
		self.assertFalse(self.saved(node_offline_seconds=0))
		self.assertFalse(self.saved(admin_timeout_seconds=0))
		self.assertFalse(self.saved(history_hours=0))
