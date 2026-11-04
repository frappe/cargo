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
	cpu_millicores: int
	ram_gb: int
	disk_gb: int

	def asdict(self) -> dict[str, Any]:
		return asdict(self)


@dataclass(frozen=True)
class PlacementRule:
	"""One Atlas affinity rule: require or reject tags on the host, or on a VM that runs on it.
	ref: https://frappe.github.io/atlas/docs/compute/placement#affinity-rules"""

	resource: Literal["metal_server", "virtual_machine"]
	operator: Literal["has", "has_not"]
	tags: dict[str, str]
	within: str = ""

	def asdict(self) -> dict[str, Any]:
		"""The rule as Atlas reads it. Atlas rejects an empty `within`, so it is left out."""
		rule = {"resource": self.resource, "operator": self.operator, "tags": self.tags}
		if self.within:
			rule["within"] = self.within

		return rule
