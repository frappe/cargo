from __future__ import annotations

import json
from typing import Any

import requests

METHOD_PREFIX = "/api/method/atlas.atlas.api.service."


class AtlasError(RuntimeError):
	"""An Atlas call failed."""

	def __init__(self, status: int, message: str) -> None:
		self.status = status
		self.message = message
		super().__init__(f"Atlas API error ({status}): {message}")


class AtlasClient:
	"""Atlas's whitelisted service API. Services subclass this to add their own calls."""

	def __init__(self, url: str, token: str, public_key: str | None = None, timeout: float = 120) -> None:
		self.url = url.rstrip("/")
		self.timeout = timeout
		self.public_key = public_key
		# Its own header: Frappe rejects any two-part `Authorization` header that does not
		# resolve to a user, before a guest endpoint is ever reached.
		self.headers = {"X-Cargo-Token": token}

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
