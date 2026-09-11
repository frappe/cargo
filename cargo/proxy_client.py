from __future__ import annotations

import time
import typing
from typing import Self

import frappe
import requests

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings

REQUEST_TIMEOUT_SECONDS = 5
ALLOWED_SITE_SUFFIX = "-svc"
MAP_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class ProxyError(RuntimeError):
	"""A Proxy control API request failed."""

	def __init__(self, message: str, *, is_retryable: bool = False) -> None:
		super().__init__(message)
		self.is_retryable = is_retryable


class ProxyClient:
	"""Manage site routes in one regional Proxy cluster."""

	def __init__(self, url: str | None, token: str | None, wildcard_domain: str | None) -> None:
		self.url = (url or "").rstrip("/")
		self.wildcard_domain = (wildcard_domain or "").strip(".").lower()
		self.headers = {"Authorization": f"Bearer {token}"}

		if not self.url or not token or not self.wildcard_domain:
			raise ProxyError("Proxy URL, token, and wildcard domain are required.")

	@classmethod
	def from_settings(cls) -> Self:
		settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")

		return cls(
			url=settings.proxy_url,
			token=settings.get_password("proxy_token"),
			wildcard_domain=settings.wildcard_domain,
		)

	def map_domain(self, domain: str, address: str) -> None:
		"""Map one domain below the regional wildcard to a mesh address. The same mapping
		written twice is the same mapping, so a transient failure is worth another try."""
		site_name = self._site_name(domain)
		if not address:
			raise ProxyError(f"{domain} has no gateway address.")

		for attempt in range(1, MAP_ATTEMPTS + 1):
			try:
				self._write_site_address(site_name, domain, address)
				return
			except ProxyError as error:
				if not error.is_retryable or attempt == MAP_ATTEMPTS:
					raise

				time.sleep(RETRY_BACKOFF_SECONDS * attempt)

	def _write_site_address(self, site_name: str, domain: str, address: str) -> None:
		"""One PATCH of a site's address, and whether its failure is worth repeating."""
		try:
			response = requests.patch(
				f"{self.url}/v1/sites/{site_name}",
				headers=self.headers,
				json={"address": address},
				timeout=REQUEST_TIMEOUT_SECONDS,
			)
		except requests.RequestException as exception:
			raise ProxyError(f"Unable to map {domain}: {exception}", is_retryable=True) from exception

		if not response.ok:
			detail = (response.text.strip() or "no response body")[:500]
			raise ProxyError(
				f"Unable to map {domain}: Proxy answered {response.status_code}: {detail}",
				is_retryable=response.status_code in RETRYABLE_STATUS_CODES,
			)

	def _site_name(self, domain: str) -> str:
		normalized_domain = domain.strip(".").lower()
		suffix = f".{self.wildcard_domain}"
		if not normalized_domain.endswith(suffix):
			raise ProxyError(f"{domain} is outside the {self.wildcard_domain} wildcard domain.")

		site_name = normalized_domain[: -len(suffix)]
		if not site_name or "." in site_name:
			raise ProxyError(f"{domain} is not one label below the wildcard domain.")
		if not site_name.endswith(ALLOWED_SITE_SUFFIX):
			raise ProxyError(f"{domain} is outside the permitted {ALLOWED_SITE_SUFFIX} site suffix.")

		return site_name
