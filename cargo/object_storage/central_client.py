from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from cargo.central_client import CentralClient as BaseCentralClient
from cargo.central_client import CentralError


class CentralClient(BaseCentralClient):
	"""Central, as object storage asks for it."""

	def get_required_credentials(
		self, region: str, vm_ids: list[str], required: Sequence[str]
	) -> dict[str, str]:
		"""The secrets every node of one cluster boots with. ``required`` is the asking
		service's own list. Idempotent per region."""
		tokens = self.call("garage_tokens", data={"region": region, "vm_ids": vm_ids})

		missing = [name for name in required if not tokens.get(name)]
		if missing:
			raise CentralError(f"Central returned no {', '.join(missing)}")

		return {name: tokens[name] for name in required}

	def register_cluster(self, region: str, base_url: str, s3_endpoint: str) -> dict[str, Any]:
		"""Tell Central the cluster is running and where to reach it."""
		return self.call(
			"register_cluster",
			data={"region": region, "base_url": base_url, "s3_endpoint": s3_endpoint},
		)
