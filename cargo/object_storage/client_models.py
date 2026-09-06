from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

Role = Literal["gateway", "storage"]

GATEWAY: Role = "gateway"
STORAGE: Role = "storage"


@dataclass
class NodeSpec:
	"""What one role's machines look like, and how many of them."""

	role: Role
	count: int
	cpu: int
	ram_gb: int
	disk_gb: int

	def asdict(self) -> dict[str, Any]:
		return asdict(self)


@dataclass
class PlacementGroupSchema:
	"""How a cluster's machines should be spread. Declared, not enforced -- Atlas has no
	placement API."""

	strategy: str
	topology_key: str
	partition_count: int
	specs: list[NodeSpec]

	@property
	def instance_count(self) -> int:
		return sum(spec.count for spec in self.specs)

	def asdict(self) -> dict[str, Any]:
		return asdict(self)
