from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

# Every role any service asks Atlas for. A machine's role is what its owner does with it,
# so they live together rather than one list per service.
Role = Literal["gateway", "storage", "telemetry", "builder"]

GATEWAY: Role = "gateway"
STORAGE: Role = "storage"
TELEMETRY: Role = "telemetry"
BUILDER: Role = "builder"


@dataclass
class NodeSpec:
	"""What one role's machines look like, and how many of them."""

	role: Role
	cpu: int
	ram_gb: int
	disk_gb: int
	count: int = 1

	def asdict(self) -> dict[str, Any]:
		return asdict(self)


@dataclass
class PlacementGroupSchema:
	"""How a service's machines should be spread. Declared, not enforced -- Atlas has no
	placement API."""

	specs: list[NodeSpec]
	strategy: str = "spread"
	topology_key: str = "zone"
	partition_count: int = 1

	@property
	def instance_count(self) -> int:
		return sum(spec.count for spec in self.specs)

	def asdict(self) -> dict[str, Any]:
		return asdict(self)
