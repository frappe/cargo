from __future__ import annotations

import typing
from typing import Any, Self

import frappe
import requests

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings

METHOD_PREFIX = "/api/method/central.api.cargo."


class CentralError(RuntimeError):
	"""A Central call failed."""


class CentralClient:
	"""Central's API. Services subclass this to add the calls they need."""

	def __init__(self, url: str, token: str, timeout: float = 30, is_bootstrapping: bool = False) -> None:
		self.url = url.rstrip("/")
		self.timeout = timeout
		# See AtlasClient: `Authorization` is unusable against a Frappe guest endpoint.
		self.headers = (
			{"X-Cargo-Token": token} if not is_bootstrapping else {"X-Cargo-Bootstrapping-Token": token}
		)

	@classmethod
	def from_settings(cls, is_bootstrapping: bool = False) -> Self:
		"""The Central client for the current site, as configured in Cargo Settings."""
		settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")
		return cls(
			url=settings.central_url,
			token=settings.get_password("central_access_token"),
			is_bootstrapping=is_bootstrapping,
		)

	def report_failure(self, region: str, step: str, error: str) -> dict[str, Any]:
		"""Tell Central a cluster it holds secrets for did not come up. Generic: every
		service fails the same way, and the shared stage runner is what reports it."""
		return self.call("report_failure", data={"region": region, "step": step, "error": error[:500]})

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
