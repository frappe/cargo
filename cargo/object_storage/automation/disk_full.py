from __future__ import annotations

import json
import math
import typing
from dataclasses import dataclass
from datetime import datetime, timedelta

import frappe
from frappe import _

from cargo.client_models import STORAGE
from cargo.object_storage.health.live import history_file

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings
	from cargo.cargo.doctype.machine.machine import Machine
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)
	from cargo.object_storage.doctype.object_storage_health_settings.object_storage_health_settings import (
		ObjectStorageHealthSettings,
	)


class DiskAtMaxCapacityError(frappe.ValidationError):
	"""A node's disk is full enough that it should be grown before it fills completely."""


@dataclass(frozen=True)
class Sample:
	"""A single reading of a node's storage usage."""

	timestamp: datetime
	used_bytes: int
	total_bytes: int


@dataclass(frozen=True)
class Extention:
	"""A single change to a node machine's capacity."""

	timestamp: datetime
	old: int
	new: int


def add_storage_node(cluster: ObjectStorageCluster) -> None:
	"""Add a storage node to the cluster with the same resources as the last node"""
	last_storage_node: Machine = cluster.storage_nodes[-1]
	cluster.add_storage_node(
		last_storage_node.cpu_millicores, last_storage_node.ram_gb, last_storage_node.disk_size_gb
	)
	cluster.setup()


class GrowStorageNodeDisks:
	"""Grows a cluster's storage disks in lockstep: Garage's capacity is its smallest disk."""

	def __init__(self, cluster: ObjectStorageCluster) -> None:
		self.cluster = cluster

	@property
	def settings(self) -> CargoSettings:
		return frappe.get_cached_doc("Cargo Settings")

	@property
	def storage_machines(self) -> list[dict]:
		return frappe.get_all(
			"Machine",
			filters={
				"reference_doctype": self.cluster.doctype,
				"reference_name": self.cluster.name,
				"role": STORAGE,
				"status": "Running",
			},
			fields=["name", "disk_size_gb"],
		)

	def grow(self) -> None:
		if not self.settings.max_storage_node_disk:
			frappe.throw(_("Set Max Storage Node Disk in Cargo Settings before growing storage disks."))

		last_extention = self.get_last_extention()
		if last_extention and frappe.utils.now_datetime() - last_extention.timestamp < timedelta(
			hours=self.settings.storage_disk_min_hours_between_growth
		):
			# This also protects us from adding storage nodes too quickly
			frappe.throw("The cluster's disks were grown recently. Please wait before growing them again.")

		current_gb = min((machine["disk_size_gb"] for machine in self.storage_machines), default=0)
		if current_gb >= self.settings.max_storage_node_disk:
			# Already at max capacity therefore handling of this exception should add a node.
			raise DiskAtMaxCapacityError(
				_("The cluster's disks are already at {0}. Please add another node.").format(
					self.settings.max_storage_node_disk
				)
			)

		disk_size_gb = self.get_required_disk_size()
		self.cluster._grow_storage_disks.run_as_workflow(disk_size_gb)

	def get_samples(self) -> list[Sample]:
		"""Usage readings of the fullest storage node, oldest first, from LiveHealth's log."""
		path = history_file()
		if not path.exists():
			return []

		machines = {machine["name"] for machine in self.storage_machines}
		samples_by_machine: dict[str, list[Sample]] = {}
		for line in path.read_text().splitlines():
			entry = json.loads(line)
			if entry.get("cluster") != self.cluster.name:
				continue

			for node in entry.get("nodes") or []:
				tags = (node.get("role") or {}).get("tags") or []
				partition = node.get("dataPartition") or {}
				if not tags or tags[0] not in machines or not partition.get("total"):
					continue

				sample = Sample(
					timestamp=datetime.fromisoformat(entry["timestamp"]),
					used_bytes=partition["total"] - partition["available"],
					total_bytes=partition["total"],
				)
				samples_by_machine.setdefault(tags[0], []).append(sample)

		if not samples_by_machine:
			return []

		# Fullest by its latest reading, as a share of its own disk.
		return max(
			samples_by_machine.values(),
			key=lambda samples: samples[-1].used_bytes / samples[-1].total_bytes,
		)

	def get_required_disk_size(self) -> int:
		"""Disk size in GB: double the last growth, or what the fullest node needs Predict Disk
		Usage Hours ahead to keep the degraded free space, whichever is larger."""
		current_gb = max((machine["disk_size_gb"] for machine in self.storage_machines), default=0)

		last_extention = self.get_last_extention()
		if last_extention:
			doubled_gb = current_gb + 2 * (last_extention.new - last_extention.old)
		else:
			doubled_gb = current_gb + math.ceil(
				current_gb * self.settings.first_storage_disk_growth_percent / 100
			)

		health_settings: ObjectStorageHealthSettings = frappe.get_cached_doc("Object Storage Health Settings")
		keep_free = health_settings.disk_degraded_percent / 100
		# Already gets the fullest node, so the prediction is for that one.
		samples = [
			(sample.timestamp.timestamp(), sample.used_bytes, sample.total_bytes)
			for sample in self.get_samples()
		]
		used_bytes = predict_linear(samples, self.settings.storage_disk_predict_hours * 3600)
		predicted_gb = math.ceil(used_bytes / (1 - keep_free) / 1024**3)

		return min(max(doubled_gb, predicted_gb), self.settings.max_storage_node_disk)

	def get_last_extention(self) -> Extention | None:
		"""The latest disk growth of the storage machine with the smallest disk."""
		machines = self.storage_machines
		if not machines:
			return None

		smallest = min(machines, key=lambda machine: machine["disk_size_gb"])
		# Newest first: the latest version may change other fields, so walk back to a disk change.
		versions = frappe.get_all(
			"Version",
			filters={
				"ref_doctype": "Machine",
				"docname": smallest["name"],
				"data": ("like", "%disk_size_gb%"),
			},
			fields=["data", "creation"],
			order_by="creation desc",
		)
		for version in versions:
			for field, old, new in json.loads(version["data"]).get("changed") or []:
				if field != "disk_size_gb":
					continue

				# Versions store values as text. A growth only: setting the size first is not one.
				old, new = int(old or 0), int(new or 0)
				if old and new > old:
					return Extention(timestamp=version["creation"], old=old, new=new)

		return None


def predict_linear(samples: list[tuple[float, int, int]], seconds_ahead: float) -> float:
	"""Used bytes `seconds_ahead` past the last sample, on the least-squares line."""
	if len(samples) < 2:
		return samples[-1][1] if samples else 0

	count = len(samples)
	mean_x = sum(sample[0] for sample in samples) / count
	mean_y = sum(sample[1] for sample in samples) / count
	spread = sum((sample[0] - mean_x) ** 2 for sample in samples)
	if not spread:
		return mean_y

	slope = sum((sample[0] - mean_x) * (sample[1] - mean_y) for sample in samples) / spread
	return mean_y + slope * (samples[-1][0] + seconds_ahead - mean_x)
