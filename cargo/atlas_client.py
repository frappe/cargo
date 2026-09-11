from __future__ import annotations

import typing
from typing import Any, Self

import frappe
import requests

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings

API_PREFIX = "/api/atlas"
RUNNING_STATE = "running"
DEAD_STATES = frozenset({"failed"})
MIB_PER_GB = 1024
# Reaches the mesh and the internet, without a public address of its own.
EGRESS = "uplink"


class AtlasError(RuntimeError):
	"""An Atlas call failed. One argument, so it survives a pickle round trip: that is how
	the workflow engine carries an exception back to the flow that raised it."""


class AtlasNotFound(AtlasError):
	"""Atlas has no such resource. A terminated machine reads as one."""


class AtlasClient:
	"""Atlas's tenant API. Every route is scoped to the tenant in the header."""

	def __init__(self, url: str, api_key: str, api_secret: str, tenant_id: int, timeout: float = 120) -> None:
		self.url = url.rstrip("/")
		self.timeout = timeout
		self.tenant_id = tenant_id
		self.headers = {
			"Authorization": f"token {api_key}:{api_secret}",
			"X-Tenant-ID": str(tenant_id),
		}

	@classmethod
	def from_settings(cls) -> Self:
		"""The Atlas client for the current site. `get_password`, not the attribute: a
		Password field reads back as its mask."""
		settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")

		return cls(
			url=settings.atlas_url,
			api_key=settings.atlas_key,
			api_secret=settings.get_password("atlas_secret"),
			tenant_id=settings.atlas_tenant_id,
		)

	def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
		"""One request against the tenant API. 204 and an empty body both return None."""
		try:
			response = requests.request(
				method,
				f"{self.url}{API_PREFIX}{path}",
				headers=self.headers,
				json=body,
				timeout=self.timeout,
			)
		except requests.RequestException as exception:
			raise AtlasError(f"{method} {path}: {exception}") from exception

		try:
			payload = response.json()
		except ValueError:
			payload = None

		if response.status_code == 404:
			raise AtlasNotFound(f"{method} {path}: {error_message(payload, response.text)}")

		if not response.ok:
			raise AtlasError(
				f"{method} {path} answered {response.status_code}: {error_message(payload, response.text)}"
			)

		return payload

	def create_vm(
		self,
		*,
		image_id: str,
		vcpus: int,
		memory_mib: int,
		disk_mib: int,
		public_key: str,
		hostname: str,
		metadata: dict[str, str] | None = None,
	) -> dict[str, Any]:
		"""Ask Atlas for one machine and return the record it made."""
		created = self.call(
			"POST",
			"/virtual-machines",
			{
				"image_id": image_id,
				"vcpus": vcpus,
				"memory_mib": memory_mib,
				"disk_mib": disk_mib,
				"ssh_keys": [public_key],
				"hostname": hostname,
				"metadata": metadata or {},
				"egress": EGRESS,
			},
		)
		if not isinstance(created, dict) or not created.get("id"):
			raise AtlasError(f"create_virtual_machine returned no id: {created!r}")

		return created

	def get_vm(self, vm_id: str) -> dict[str, Any]:
		"""The VM as Atlas currently sees it, including `current_state`."""
		return self.call("GET", f"/virtual-machines/{vm_id}")

	def terminate_vm(self, vm_id: str) -> None:
		"""Start termination. The VM route answers 404 once cleanup finishes."""
		self.call("DELETE", f"/virtual-machines/{vm_id}")

	def create_snapshot(self, vm_id: str, title: str) -> str:
		"""Freeze a machine's disk into an image Atlas can boot later."""
		created = self.call("POST", f"/virtual-machines/{vm_id}/actions/snapshot", {"title": title})
		if not isinstance(created, dict) or not created.get("id"):
			raise AtlasError(f"create_snapshot returned no id: {created!r}")

		return created["id"]

	def get_snapshot(self, image_id: str) -> dict[str, Any]:
		"""The image as Atlas currently sees it, to know when it is usable."""
		return self.call("GET", f"/images/{image_id}")


def host_port(address: str, port: int | str) -> str:
	"""``address:port``, with the brackets an IPv6 address needs to keep its colons."""
	return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


def error_message(payload: Any, fallback: str) -> str:
	"""The readable message out of an Atlas or Frappe error body."""
	if isinstance(payload, dict):
		error = payload.get("error")
		if isinstance(error, dict) and error.get("message"):
			fields = "; ".join(
				f"{field.get('name')}: {field.get('message')}"
				for field in error.get("fields") or []
				if isinstance(field, dict)
			)
			return f"{error['message']} ({fields})" if fields else str(error["message"])

		for key in ("exception", "exc_type", "message", "_error_message"):
			if payload.get(key):
				return str(payload[key])

	return (fallback or "").strip() or "unknown error"
