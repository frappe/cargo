from __future__ import annotations

import typing
from dataclasses import dataclass

import frappe

from cargo.garage_admin_client import GarageAdminClient, GarageError

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

UNKNOWN, HEALTHY, DEGRADED, CRITICAL = "Unknown", "Healthy", "Degraded", "Critical"
SEVERITY = (UNKNOWN, HEALTHY, DEGRADED, CRITICAL)

DISK_DEGRADED_PERCENT = 20
DISK_CRITICAL_PERCENT = 10
NODE_OFFLINE_SECONDS = 120
ADMIN_TIMEOUT = 5


@dataclass(frozen=True)
class Finding:
	"""One thing wrong with a cluster, in words an operator can act on."""

	severity: str
	reason: str


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
		self.admin = GarageAdminClient.for_cluster(self.cluster)
		self.admin.timeout = ADMIN_TIMEOUT

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

	def findings(self) -> list[Finding]:
		"""Everything the cluster has against it, from one read of the gateway.

		A gateway that cannot be reached is the only finding: the rest would be invented
		from data we do not have."""
		try:
			health, status = self.admin.health(), self.admin.status()
		except GarageError as error:
			return [Finding(CRITICAL, f"the gateway's admin API could not be reached: {error}")]

		nodes = status.get("nodes") or []

		return [*self.quorum_findings(health), *self.node_findings(nodes)]

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
				if seen is None or seen >= NODE_OFFLINE_SECONDS:
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
			if free < DISK_CRITICAL_PERCENT:
				findings.append(Finding(CRITICAL, f"{machine} has {free}% free on its {volume} volume"))
			elif free < DISK_DEGRADED_PERCENT:
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

		return finding
