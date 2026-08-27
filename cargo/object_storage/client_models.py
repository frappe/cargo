from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

Role = Literal["gateway", "storage"]

GATEWAY: Role = "gateway"
STORAGE: Role = "storage"


@dataclass
class NodeSpec:
	"""The shape of one machine."""

	cpu: int
	ram_gb: int
	disk_gb: int

	def asdict(self) -> dict[str, Any]:
		return asdict(self)


@dataclass
class PlacementGroupSchema:
	"""How a cluster's machines should be spread.

	Declared, not enforced: Atlas has no placement API, so this is carried whole until it
	does. See `docs/atlas-contract.md`.
	"""

	strategy: str
	topology_key: str
	partition_count: int
	instance_count: int
	spec: NodeSpec

	def asdict(self) -> dict[str, Any]:
		return asdict(self)
