from __future__ import annotations

import json
import math
import typing
from dataclasses import dataclass
from datetime import datetime

import frappe
from frappe import _

from cargo.client_models import STORAGE
from cargo.object_storage.health.live import history_file

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)
	from cargo.object_storage.doctype.object_storage_health_settings.object_storage_health_settings import (
		ObjectStorageHealthSettings,
	)


class AddStorageNode: ...


@dataclass(frozen=True)
class Extention:
	"""One disk growth, read from the Machine's version history."""

	machine: str
	on: datetime
	from_gb: int
	to_gb: int

	@property
	def size_gb(self) -> int:
		return self.to_gb - self.from_gb


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

	@property
	def disk_size_gb(self) -> int:
		return max((machine["disk_size_gb"] for machine in self.storage_machines), default=0)

	def grow(self, extend_by: int | None = None) -> str:
		extentions = self.get_last_extentions()
		last_extention_size = extentions[0].size_gb if extentions else None
		return self.grow_disks(self.get_extention_size(last_extention_size, extend_by))

	def get_last_extentions(self) -> list[Extention]:
		"""Disk growths of this cluster's storage machines, newest first."""
		machines = [machine["name"] for machine in self.storage_machines]
		if not machines:
			return []

		versions = frappe.get_all(
			"Version",
			filters={"ref_doctype": "Machine", "docname": ("in", machines)},
			fields=["docname", "creation", "data"],
			order_by="creation desc",
		)

		extentions = []
		for version in versions:
			for field, before, after in json.loads(version.data).get("changed") or []:
				if field == "disk_size_gb" and (after or 0) > (before or 0):
					extentions.append(Extention(version.docname, version.creation, before, after))

		return extentions

	def get_extention_size(self, last_extention_size: int | None = None, extend_by: int | None = None) -> int:
		"""`extend_by`, else the larger of double the last growth and the predicted shortfall, capped at Max Storage Node Disk."""
		current = self.disk_size_gb
		if extend_by:
			size = int(extend_by)
		else:
			doubled = (
				2 * last_extention_size
				if last_extention_size
				else math.ceil(current * self.settings.first_storage_disk_growth_percent / 100)
			)
			size = max(doubled, self.get_predicted_shortfall_gb())

		capped_growth = max(0, min(size, self.max_disk_size_gb - current))
		if capped_growth == 0:
			frappe.throw(_("Storage disks are at Max Storage Node Disk. Add a storage node instead."))

		return capped_growth

	@property
	def max_disk_size_gb(self) -> int:
		if not self.settings.max_storage_node_disk:
			frappe.throw(_("Set Max Storage Node Disk in Cargo Settings before growing storage disks."))

		return self.settings.max_storage_node_disk

	def get_predicted_shortfall_gb(self) -> int:
		"""GB the fullest node lacks, Predict Disk Usage Hours ahead, to keep the degraded percent free."""
		settings: ObjectStorageHealthSettings = frappe.get_cached_doc("Object Storage Health Settings")
		keep_free = settings.disk_degraded_percent / 100

		shortfall = 0
		for samples in self.get_usage_samples().values():
			total = samples[-1][2]
			needed = predict_linear(samples, self.settings.storage_disk_predict_hours * 3600) / (
				1 - keep_free
			)
			shortfall = max(shortfall, math.ceil((needed - total) / 1024**3))

		return shortfall

	def get_usage_samples(self) -> dict[str, list[tuple[float, int, int]]]:
		"""(seconds, used bytes, total bytes) per storage machine, oldest first, from LiveHealth's log."""
		path = history_file()
		if not path.exists():
			return {}

		machines = {machine["name"] for machine in self.storage_machines}
		samples: dict[str, list[tuple[float, int, int]]] = {}
		for line in path.read_text().splitlines():
			entry = json.loads(line)
			if entry.get("cluster") != self.cluster.name:
				continue

			seconds = datetime.fromisoformat(entry["timestamp"]).timestamp()
			for node in entry.get("nodes") or []:
				tags = (node.get("role") or {}).get("tags") or []
				partition = node.get("dataPartition") or {}
				if not tags or tags[0] not in machines or not partition.get("total"):
					continue

				used = partition["total"] - partition["available"]
				samples.setdefault(tags[0], []).append((seconds, used, partition["total"]))

		return samples

	def grow_disks(self, size: int) -> str:
		"""Start the workflow taking every storage disk to the largest one plus `size` GB."""
		from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
			ensure_no_running_workflow,
		)

		if size <= 0:
			frappe.throw(_("Storage disks are at Max Storage Node Disk. Add a storage node instead."))
		if self.cluster.status != "Active":
			frappe.throw(_("Only an active cluster's disks are grown."))
		ensure_no_running_workflow(self.cluster)

		return self.cluster._grow_storage_disks.run_as_workflow(self.disk_size_gb + size)


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
