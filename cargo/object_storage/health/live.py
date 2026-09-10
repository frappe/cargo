from __future__ import annotations

import json
import typing
from dataclasses import dataclass
from functools import cached_property
from itertools import dropwhile
from pathlib import Path

import frappe
from frappe.utils.synchronization import filelock

from cargo.object_storage.garage.client import Client, Error

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)
	from cargo.object_storage.doctype.object_storage_health_settings.object_storage_health_settings import (
		ObjectStorageHealthSettings,
	)

UNKNOWN, HEALTHY, DEGRADED, CRITICAL = "Unknown", "Healthy", "Degraded", "Critical"
SEVERITY = (UNKNOWN, HEALTHY, DEGRADED, CRITICAL)

HISTORY_FILE = "cluster_health.json.log"
HISTORY_LOCK = "cluster_health_log"


@dataclass(frozen=True)
class Finding:
	"""One thing wrong with a cluster, in words an operator can act on."""

	severity: str
	reason: str


@dataclass(frozen=True)
class Reading:
	"""What the gateway said, or why it said nothing."""

	health: dict
	nodes: list[dict]
	error: str = ""


class LiveHealth(Client):
	"""Check live critical metrics of a cluster, by talking to the admin api
	In case of any error, fire off webhooks to alert someone. This is a safety in case
	the metrics/telemetry go down, also metrics and telemetry might be only used for postmortem

	Tracking the following:
		- Is node up.
		- Node disk space.
		- Node lastSeenSecAgo
		- Quorum satisfied
	"""

	def __init__(self, cluster: ObjectStorageCluster) -> None:
		super().__init__(cluster)
		self.settings: ObjectStorageHealthSettings = frappe.get_cached_doc("Object Storage Health Settings")
		# Health runs every minute, so a hung gateway must not still be waiting on the next tick.
		self.timeout = self.settings.admin_timeout_seconds

	def check(self) -> Finding:
		"""The worst thing true about this cluster right now, and why."""
		if self.cluster.status == "Failed":
			return Finding(CRITICAL, "the build failed, so nothing is serving")

		# A cluster still being built is not judged: it has not promised anything yet.
		if not self.cluster.is_live:
			return Finding(UNKNOWN, "")

		return max(
			self.findings(),
			key=lambda finding: SEVERITY.index(finding.severity),
			default=Finding(HEALTHY, ""),
		)

	@cached_property
	def reading(self) -> Reading:
		"""One read of the gateway, shared by every check and by the log line."""
		try:
			return Reading(health=self.health(), nodes=self.status().get("nodes") or [])
		except Error as error:
			return Reading(health={}, nodes=[], error=str(error))

	def findings(self) -> list[Finding]:
		"""Everything the cluster has against it. An unreachable gateway is the only
		finding: the rest would be invented from data we do not have."""
		reading = self.reading
		if reading.error:
			return [Finding(CRITICAL, f"the gateway's admin API could not be reached: {reading.error}")]

		return [*self.quorum_findings(reading.health), *self.node_findings(reading.nodes)]

	def quorum_findings(self, health: dict) -> list[Finding]:
		"""Partitions are the keyspace, not the nodes. Losing quorum on some means the objects
		living there cannot be written, which is why it outranks a node being down."""
		partitions = health.get("partitions") or 0
		if not partitions:
			return []

		if (quorum := health.get("partitionsQuorum") or 0) < partitions:
			return [Finding(CRITICAL, f"{partitions - quorum} of {partitions} partitions cannot be written")]

		if (all_ok := health.get("partitionsAllOk") or 0) < partitions:
			return [Finding(DEGRADED, f"{partitions - all_ok} of {partitions} partitions are missing a copy")]

		return []

	def node_findings(self, nodes: list[dict]) -> list[Finding]:
		"""Named by machine, not Garage node id: the tag setup wrote is what an operator can
		look up. A node with no role is not in the layout yet."""
		findings = []
		for node in nodes:
			tags = (node.get("role") or {}).get("tags") or []
			if not tags:
				continue

			machine = tags[0]
			if not node.get("isUp"):
				seen = node.get("lastSeenSecsAgo")
				if seen is None or seen >= self.settings.node_offline_seconds:
					findings.append(Finding(DEGRADED, f"{machine} has been unreachable for {seen or 0}s"))
				continue

			findings.extend(self.disk_findings(machine, node))

		return findings

	def disk_findings(self, machine: str, node: dict) -> list[Finding]:
		"""Both volumes matter: Garage stops accepting writes when either fills."""
		findings = []
		for volume, key in (("data", "dataPartition"), ("metadata", "metadataPartition")):
			partition = node.get(key) or {}
			total, available = partition.get("total") or 0, partition.get("available") or 0
			if not total:
				continue

			free = available * 100 // total
			if free < self.settings.disk_critical_percent:
				findings.append(Finding(CRITICAL, f"{machine} has {free}% free on its {volume} volume"))
			elif free < self.settings.disk_degraded_percent:
				findings.append(Finding(DEGRADED, f"{machine} has {free}% free on its {volume} volume"))

		return findings

	def record(self) -> Finding:
		"""Write the verdict onto the cluster, and say so loudly when it is Critical."""
		finding = self.check()
		if (self.cluster.health, self.cluster.health_reason) != (finding.severity, finding.reason):
			self.cluster.db_set({"health": finding.severity, "health_reason": finding.reason}, notify=True)
			if finding.severity == CRITICAL:
				frappe.log_error(title=f"{self.cluster.name} is critical", message=finding.reason)

		self.dump(finding)

		return finding

	def dump(self, finding: Finding) -> None:
		"""Append this reading to the log. Kept only so there is something to read after the
		fact when datum was unreachable; `prune_history` drops it once it is old."""
		try:
			# Stamped under the lock, so the log stays ordered and `prune_history` can stop
			# at the first line still inside the window.
			with filelock(HISTORY_LOCK, is_global=True, timeout=5):
				entry = {
					"timestamp": frappe.utils.now_datetime().isoformat(),
					"cluster": self.cluster.name,
					"severity": finding.severity,
					"reason": finding.reason,
					"error": self.reading.error,
					"health": self.reading.health,
					"nodes": self.reading.nodes,
				}
				with history_file().open("a") as log:
					log.write(json.dumps(entry) + "\n")
		except Exception:
			# The verdict on the cluster matters more than the copy of it.
			frappe.log_error(title="Could not write cluster health log")


def history_file() -> Path:
	return Path(frappe.utils.get_bench_path()) / "logs" / HISTORY_FILE


def prune_history() -> None:
	"""Drop readings past the window. Scheduled hourly."""
	path = history_file()
	if not path.exists():
		return

	hours = frappe.get_cached_doc("Object Storage Health Settings").history_hours
	cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-hours).isoformat()

	try:
		with filelock(HISTORY_LOCK, is_global=True, timeout=30):
			# Appended in order, so everything expired is at the front.
			kept = list(
				dropwhile(lambda line: json.loads(line)["timestamp"] < cutoff, path.read_text().splitlines())
			)
			# Written whole and renamed over, so a crash leaves the old log rather than a torn line.
			scratch = path.with_suffix(".tmp")
			scratch.write_text("\n".join(kept) + "\n" if kept else "")
			scratch.replace(path)
	except Exception:
		frappe.log_error(title="Could not prune cluster health log")
