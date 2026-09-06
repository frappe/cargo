from __future__ import annotations

import typing
from dataclasses import dataclass
from typing import Self

import frappe

from cargo.object_storage.client_models import STORAGE

UNKNOWN = "Unknown"
HEALTHY = "Healthy"
DEGRADED = "Degraded"
CRITICAL = "Critical"
#: Worst last, so the worst finding wins.
SEVERITY = (HEALTHY, DEGRADED, CRITICAL)

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)


@dataclass(frozen=True)
class Finding:
	"""One thing wrong with a cluster, in the words the operator reads."""

	severity: str
	reason: str


@dataclass(frozen=True)
class ClusterFacts:
	"""One read of the machines, so every check does not query for itself."""

	cluster: ObjectStorageCluster
	machines: list[dict]

	@classmethod
	def of(cls, cluster: ObjectStorageCluster) -> Self:
		return cls(
			cluster=cluster,
			machines=frappe.get_all(
				"Machine",
				filters={"name": ("in", cluster.associated_machines)},
				fields=["name", "role", "status"],
			),
		)

	@property
	def running(self) -> list[dict]:
		return [machine for machine in self.machines if machine.status == "Running"]

	@property
	def running_storage(self) -> list[dict]:
		return [machine for machine in self.running if machine.role == STORAGE]


class Check:
	"""One rule about a cluster. It reports only when something is wrong."""

	def run(self, facts: ClusterFacts) -> Finding | None:
		raise NotImplementedError


class GatewayCheck(Check):
	"""Every S3 request arrives through the gateway, so a cluster without one serves nothing."""

	def run(self, facts: ClusterFacts) -> Finding | None:
		gateway = facts.cluster.gateway_node
		if not gateway:
			return Finding(CRITICAL, "this cluster has no gateway machine")

		if gateway in {machine.name for machine in facts.running}:
			return None

		return Finding(CRITICAL, f"the gateway {gateway} is not running")


class ReplicaCheck(Check):
	"""Garage needs one node per copy: below that it cannot write, whatever else is up."""

	def run(self, facts: ClusterFacts) -> Finding | None:
		running = len(facts.running_storage)
		if running >= facts.cluster.replication_factor:
			return None

		return Finding(
			CRITICAL,
			f"{running} storage nodes running, {facts.cluster.replication_factor} needed for a full copy",
		)


class MachineCheck(Check):
	"""A cluster short of a machine still serves, on fewer nodes than it was built for."""

	def run(self, facts: ClusterFacts) -> Finding | None:
		down = len(facts.machines) - len(facts.running)
		if not down:
			return None

		return Finding(DEGRADED, f"{down} of {len(facts.machines)} machines are not running")


#: Database only: cheap enough to run on every save.
CHEAP_CHECKS: tuple[Check, ...] = (GatewayCheck(), ReplicaCheck(), MachineCheck())
#: Reach out to the cluster itself. Scheduled, never on save. Disk and packet loss land here.
NETWORK_CHECKS: tuple[Check, ...] = ()
