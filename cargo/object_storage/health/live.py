from __future__ import annotations

import json
import typing
from dataclasses import dataclass
from functools import cached_property
from itertools import dropwhile
from pathlib import Path

import frappe
from frappe.utils.synchronization import filelock

from cargo.garage_admin_client import GarageAdminClient, GarageError

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)
	from cargo.object_storage.doctype.object_storage_health_settings.object_storage_health_settings import (
		ObjectStorageHealthSettings,
	)

UNKNOWN, HEALTHY, DEGRADED, CRITICAL = "Unknown", "Healthy", "Degraded", "Critical"
# Worst last: a run's verdict is the highest severity anything reported.
SEVERITY = (UNKNOWN, HEALTHY, DEGRADED, CRITICAL)

# The name of the log, not a tuning knob: how long it is kept is in the settings.
HISTORY_FILE = "cluster_health.json.log"


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


class LiveHealth:
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
		self.cluster = cluster
		self.settings: ObjectStorageHealthSettings = frappe.get_cached_doc("Object Storage Health Settings")
		self.admin = GarageAdminClient.for_cluster(self.cluster)
		self.admin.timeout = self.settings.admin_timeout_seconds

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
		"""One read of the gateway, shared by every check and by the history line.

		Cached for the life of this object: a check that re-asked would be judging a
		different moment than the one beside it."""
		try:
			return Reading(health=self.admin.health(), nodes=self.admin.status().get("nodes") or [])
		except GarageError as error:
			return Reading(health={}, nodes=[], error=str(error))

	def findings(self) -> list[Finding]:
		"""Everything the cluster has against it, from one read of the gateway.

		A gateway that cannot be reached is the only finding: the rest would be invented
		from data we do not have."""
		reading = self.reading
		if reading.error:
			# If gateway does not respond we immediately know that cluster is in critical state.
			return [Finding(CRITICAL, f"the gateway's admin API could not be reached: {reading.error}")]

		return [*self.quorum_findings(reading.health), *self.node_findings(reading.nodes)]

	def quorum_findings(self, health: dict) -> list[Finding]:
		"""Partitions are the keyspace, not the nodes: 256 of them regardless of cluster size.

		Losing quorum on some means the objects living there cannot be written, which is why
		it outranks a node being down. `partitionsAllOk` is the weaker signal -- every replica
		present -- and only differs from quorum above `replication_factor` 2."""
		partitions = health.get("partitions") or 0
		if not partitions:
			return []

		if (quorum := health.get("partitionsQuorum") or 0) < partitions:
			return [Finding(CRITICAL, f"{partitions - quorum} of {partitions} partitions cannot be written")]

		if (all_ok := health.get("partitionsAllOk") or 0) < partitions:
			return [Finding(DEGRADED, f"{partitions - all_ok} of {partitions} partitions are missing a copy")]

		return []

	def node_findings(self, nodes: list[dict]) -> list[Finding]:
		"""Findings name the machine, not the Garage node id: the tag setup wrote is the
		only thing an operator can look up. A node with no role is not in the layout yet."""
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
		"""Both volumes matter: Garage stops accepting writes when either fills, and on most
		of these nodes they are the same filesystem reported twice."""
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
		"""Write the verdict onto the cluster, and say so loudly when it is Critical.

		db_set rather than save: health is a reading about the cluster, not a change to it,
		and a save here would re-run the cluster's own hooks on a one-minute clock."""
		finding = self.check()
		if (self.cluster.health, self.cluster.health_reason) != (finding.severity, finding.reason):
			self.cluster.db_set({"health": finding.severity, "health_reason": finding.reason}, notify=True)
			if finding.severity == CRITICAL:
				# The seam alerting will hang off. Until then this is what pages a human.
				frappe.log_error(title=f"{self.cluster.name} is critical", message=finding.reason)

		self.dump(finding)

		return finding

	def dump(self, finding: Finding) -> None:
		"""Append this reading to the log and drop anything past the window.

		Kept only so there is something to read after the fact when datum was unreachable.
		Nothing queries it: no index, no rotation, no viewer."""
		path = Path(frappe.utils.get_bench_path()) / "logs" / HISTORY_FILE
		now = frappe.utils.now_datetime()
		cutoff = frappe.utils.add_to_date(now, hours=-self.settings.history_hours).isoformat()
		entry = {
			"timestamp": now.isoformat(),
			"cluster": self.cluster.name,
			"severity": finding.severity,
			"reason": finding.reason,
			"error": self.reading.error,
			"health": self.reading.health,
			"nodes": self.reading.nodes,
		}

		try:
			with filelock("cluster_health_dump", is_global=True, timeout=5):
				# Appended in order, so everything expired is at the front: stop at the first
				# line still inside the window and keep the rest as written.
				existing = path.read_text().splitlines() if path.exists() else []
				kept = dropwhile(lambda line: json.loads(line)["timestamp"] < cutoff, existing)
				# Written whole and renamed over: a crash mid-write leaves the old log, not a
				# torn line the next run would choke on.
				scratch = path.with_suffix(".tmp")
				scratch.write_text("\n".join([*kept, json.dumps(entry)]) + "\n")
				scratch.replace(path)
		except Exception:
			# The verdict on the cluster matters more than the copy of it.
			frappe.log_error(title="Could not write cluster health log")
