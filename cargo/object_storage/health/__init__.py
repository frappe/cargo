from __future__ import annotations

import typing

import frappe

from cargo.object_storage.health.checks import (
	CHEAP_CHECKS,
	CRITICAL,
	HEALTHY,
	NETWORK_CHECKS,
	SEVERITY,
	UNKNOWN,
	Check,
	ClusterFacts,
	Finding,
)

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)


class ClusterHealth:
	"""What users get from a cluster right now, as opposed to what Cargo is doing to it."""

	def __init__(self, cluster: ObjectStorageCluster) -> None:
		self.cluster = cluster

	def checks(self, cheap_only: bool) -> tuple[Check, ...]:
		"""Saving a document may only ask the database. A scheduled run may ask the cluster."""
		return CHEAP_CHECKS if cheap_only else CHEAP_CHECKS + NETWORK_CHECKS

	def findings(self, cheap_only: bool = True) -> list[Finding]:
		"""Everything the checks have against this cluster, whether or not it is serving yet."""
		facts = ClusterFacts.of(self.cluster)

		return [finding for check in self.checks(cheap_only) if (finding := check.run(facts))]

	def blockers(self, cheap_only: bool = True) -> list[Finding]:
		"""What stands between this cluster and serving S3."""
		return [finding for finding in self.findings(cheap_only) if finding.severity == CRITICAL]

	def evaluate(self, cheap_only: bool = True) -> Finding:
		"""The worst thing true about this cluster right now, and why."""
		if self.cluster.status == "Failed":
			return Finding(CRITICAL, "the build failed, so nothing is serving")

		# A cluster still being built is not judged: it has not promised anything yet.
		if not self.cluster.lifecycle.is_live:
			return Finding(UNKNOWN, "")

		return max(
			self.findings(cheap_only),
			key=lambda finding: SEVERITY.index(finding.severity),
			default=Finding(HEALTHY, ""),
		)


def refresh_health() -> None:
	"""Recompute every live cluster's health. Scheduled in `hooks.py`."""
	for name in frappe.get_all(
		"Object Storage Cluster", filters={"activated_on": ("is", "set")}, pluck="name"
	):
		cluster: ObjectStorageCluster = frappe.get_doc("Object Storage Cluster", name)
		finding = ClusterHealth(cluster).evaluate()
		if (cluster.health, cluster.health_reason) == (finding.severity, finding.reason):
			continue

		# The save recomputes health itself; this only decides whether one is worth writing.
		cluster.save(ignore_permissions=True)
