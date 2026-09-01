from __future__ import annotations

import json
import typing
from typing import Any, Self

import frappe
import requests

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings
	from cargo.object_storage.client_models import PlacementGroupSchema

METHOD_PREFIX = "/api/method/atlas.atlas.api.service."


class AtlasError(RuntimeError):
	"""An Atlas call failed."""

	def __init__(self, status: int, message: str) -> None:
		self.status = status
		self.message = message
		super().__init__(f"Atlas API error ({status}): {message}")


class AtlasClient:
	"""Atlas's whitelisted service API. Services subclass this to add their own calls."""

	def __init__(self, url: str, token: str, timeout: float = 120) -> None:
		self.url = url.rstrip("/")
		self.timeout = timeout
		self.headers = {"X-Cargo-Token": token}

	@classmethod
	def from_settings(cls) -> Self:
		"""The Atlas client for the current site, as configured in Cargo Settings.

		The token is a Password field: reading the attribute gives the mask, not the secret."""
		settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")

		return cls(url=settings.atlas_url, token=settings.get_password("atlas_access_token"))

	def call(self, endpoint: str, **params: Any) -> Any:
		"""POST to a service method and return the unwrapped ``message``."""
		try:
			response = requests.post(
				f"{self.url}{METHOD_PREFIX}{endpoint}",
				headers=self.headers,
				json={key: value for key, value in params.items() if value is not None},
				timeout=self.timeout,
			)
		except requests.RequestException as exception:
			raise AtlasError(0, f"{endpoint}: {exception}") from exception

		try:
			payload = response.json()
		except ValueError:
			payload = None

		if not response.ok:
			raise AtlasError(response.status_code, error_message(payload, response.text))

		if isinstance(payload, dict) and (payload.get("exc") or payload.get("exception")):
			raise AtlasError(response.status_code, error_message(payload, response.text))

		return payload["message"] if isinstance(payload, dict) and "message" in payload else payload

	def create_bare_vms(
		self,
		title: str,
		*,
		public_key: str,
		base_image: str,
		placement: dict[str, Any] | None = None,
	) -> list[str]:
		"""Ask Atlas for machines and return their VM ids. They are still booting and have no
		address. ``placement`` is dropped when a service does not care where they land."""
		created = self.call(
			"create_bare_vms",
			title=title,
			base_image=base_image,
			ssh_public_key=public_key,
			placement_group=placement,
		)
		vm_ids = created.get("vm_ids") if isinstance(created, dict) else created
		if not vm_ids:
			raise AtlasError(0, f"create_bare_vms returned no VM ids: {created!r}")

		return list(vm_ids)

	def create_vms(
		self,
		title: str,
		placement: PlacementGroupSchema,
		*,
		public_key: str,
		base_image: str = "ubuntu-22.04",
	) -> list[str]:
		"""Object storage: a placement group's machines, so its nodes do not share a host."""
		return self.create_bare_vms(
			title, public_key=public_key, base_image=base_image, placement=placement.asdict()
		)

	def create_vm(self, title: str, *, public_key: str, base_image: str = "ubuntu-24.04") -> str:
		"""Image builder: one machine to bake. Nothing cares where it lands, so it asks for no
		placement."""
		vm_ids = self.create_bare_vms(title, public_key=public_key, base_image=base_image)

		return vm_ids[0]

	def create_snapshot(self, vm_id: str, title: str) -> str:
		"""Freeze a machine's disk into an image Atlas can boot later."""
		created = self.call("create_snapshot", vm=vm_id, title=title)
		snapshot = created.get("snapshot_id") if isinstance(created, dict) else created
		if not snapshot:
			raise AtlasError(0, f"create_snapshot returned no id: {created!r}")

		return str(snapshot)

	def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
		"""The snapshot as Atlas currently sees it, to know when it is usable."""
		return self.call("get_snapshot", snapshot=snapshot_id)

	def get_vm(self, vm_id: str) -> dict[str, Any]:
		"""The VM as Atlas currently sees it."""
		return self.call("get_virtual_machine", name=vm_id)

	def terminate_vm(self, name: str) -> dict[str, Any] | None:
		return self.call("terminate_vm", vm=name)


def error_message(payload: Any, fallback: str) -> str:
	"""The readable message out of a Frappe error body."""
	if isinstance(payload, dict):
		messages = payload.get("_server_messages")
		if messages:
			try:
				parsed = json.loads(messages)
				texts = [json.loads(m).get("message", m) if isinstance(m, str) else str(m) for m in parsed]
				if texts:
					return "; ".join(str(text) for text in texts)
			except (ValueError, TypeError):
				return str(messages)

		for key in ("exception", "exc_type", "message", "_error_message", "error"):
			if payload.get(key):
				return str(payload[key])

	return (fallback or "").strip() or "unknown error"
