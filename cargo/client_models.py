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
	"""What one role's machines look like."""

	role: Role
	cpu: int
	ram_gb: int
	disk_gb: int

	def asdict(self) -> dict[str, Any]:
		return asdict(self)
