from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import requests


class CentralError(RuntimeError):
	"""A Central call failed."""


class CentralClient:
	"""Central's API, which issues the secrets a cluster runs on."""

	def __init__(self, url: str, token: str, timeout: float = 30) -> None:
		self.url = url.rstrip("/")
		self.timeout = timeout
		self.headers = {"X-Cargo-Token": token}

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
		"""Tell Central the cluster is running and where to reach it. Until this lands
		Central holds the secrets but cannot create a bucket."""
		return self.call(
			"register_cluster",
			data={"region": region, "base_url": base_url, "s3_endpoint": s3_endpoint},
		)

	def report_failure(self, region: str, step: str, error: str) -> dict[str, Any]:
		"""Tell Central the cluster it holds secrets for did not come up."""
		return self.call("report_failure", data={"region": region, "step": step, "error": error[:500]})

	def call(self, endpoint: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
		try:
			response = requests.post(
				f"{self.url}/api/method/central.api.cargo.{endpoint}",
				headers=self.headers,
				json=data,
				timeout=self.timeout,
			)
		except requests.RequestException as exception:
			raise CentralError(f"{endpoint}: {exception}") from exception

		if not response.ok:
			raise CentralError(f"{endpoint} failed ({response.status_code}): {response.text[:300]}")

		payload = response.json()

		return payload.get("message", payload) if isinstance(payload, dict) else payload
