from __future__ import annotations

from typing import Any

import requests

METHOD_PREFIX = "/api/method/central.api.cargo."


class CentralError(RuntimeError):
	"""A Central call failed."""


class CentralClient:
	"""Central's API. Services subclass this to add the calls they need."""

	def __init__(self, url: str, token: str, timeout: float = 30) -> None:
		self.url = url.rstrip("/")
		self.timeout = timeout
		# See AtlasClient: `Authorization` is unusable against a Frappe guest endpoint.
		self.headers = {"X-Cargo-Token": token}

	def call(self, endpoint: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
		try:
			response = requests.post(
				f"{self.url}{METHOD_PREFIX}{endpoint}",
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
