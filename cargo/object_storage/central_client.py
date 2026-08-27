from __future__ import annotations

from typing import Any

import requests

REQUIRED_TOKENS = ("rpc_secret", "admin_token", "metrics_token")


class CentralError(RuntimeError):
	"""A Central call failed."""


class CentralClient:
	"""Central's API, which issues the secrets a cluster runs on."""

	def __init__(self, url: str, token: str, timeout: float = 30) -> None:
		self.url = url.rstrip("/")
		self.timeout = timeout
		self.headers = {"Authorization": f"Bearer {token}"}

	def get_required_credentials(self, region: str, vm_ids: list[str]) -> dict[str, str]:
		"""The secrets every node of one cluster boots with.

		Idempotent per region, so a retry cannot split a cluster into nodes that fail to
		recognise each other.
		"""
		tokens = self.call("garage_tokens", data={"region": region, "vm_ids": vm_ids})

		missing = [name for name in REQUIRED_TOKENS if not tokens.get(name)]
		if missing:
			raise CentralError(f"Central returned no {', '.join(missing)}")

		return {name: tokens[name] for name in REQUIRED_TOKENS}

	def call(self, endpoint: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
		try:
			response = requests.post(
				f"{self.url}/api/method/central.api.atlas.{endpoint}",
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
