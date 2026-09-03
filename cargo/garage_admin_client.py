from __future__ import annotations

import typing
from typing import Any, Self

import requests

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

API_PREFIX = "/v2/"
READ_WRITE = {"read": True, "write": True, "owner": False}


class GarageError(RuntimeError):
	"""A Garage Admin API call failed."""

	def __init__(self, status: int, message: str) -> None:
		self.status = status
		self.message = message
		super().__init__(f"Garage API error ({status}): {message}")


class GarageAdminClient:
	"""Garage's Admin API v2. Object storage itself goes to the S3 endpoint, not here."""

	def __init__(self, base_url: str, admin_token: str, timeout: float = 30) -> None:
		self.url = base_url.rstrip("/")
		self.timeout = timeout
		self.headers = {"Authorization": f"Bearer {admin_token}"}

	@classmethod
	def for_cluster(cls, cluster: ObjectStorageCluster) -> Self:
		"""Every node serves the same API, so the gateway answers for the cluster."""
		return cls(
			f"http://{cluster.gateway_address()}:{cluster.admin_port}",
			cluster.get_password("admin_token"),
		)

	def call(self, endpoint: str, method: str = "GET", **kwargs: Any) -> Any:
		try:
			response = requests.request(
				method,
				f"{self.url}{API_PREFIX}{endpoint}",
				headers=self.headers,
				timeout=self.timeout,
				**kwargs,
			)
		except requests.RequestException as exception:
			raise GarageError(0, f"{endpoint}: {exception}") from exception

		if response.status_code == 404:
			return None

		if not response.ok:
			raise GarageError(response.status_code, f"{endpoint}: {response.text[:500]}")

		return response.json() if response.content else None

	def status(self) -> dict[str, Any]:
		return self.call("GetClusterStatus")

	def layout(self) -> dict[str, Any]:
		return self.call("GetClusterLayout")

	def assign_roles(self, roles: list[dict[str, Any]]) -> dict[str, Any]:
		"""Stage one role per node. Nothing takes effect until `apply_layout`."""
		return self.call("UpdateClusterLayout", "POST", json={"roles": roles})

	def apply_layout(self, version: int) -> dict[str, Any]:
		return self.call("ApplyClusterLayout", "POST", json={"version": version})

	def bucket(self, alias: str) -> dict[str, Any] | None:
		return self.call("GetBucketInfo", params={"globalAlias": alias})

	def create_bucket(self, alias: str) -> dict[str, Any]:
		return self.call("CreateBucket", "POST", json={"globalAlias": alias})

	def key(self, name: str) -> dict[str, Any] | None:
		"""The key by exact name, secret included. `search` is a prefix match, so the name
		is checked again here."""
		found = self.call("GetKeyInfo", params={"search": name, "showSecretKey": "true"})

		return found if found and found.get("name") == name else None

	def create_key(self, name: str) -> dict[str, Any]:
		return self.call("CreateKey", "POST", json={"name": name})

	def allow_bucket_key(self, bucket_id: str, access_key_id: str) -> dict[str, Any]:
		return self.call(
			"AllowBucketKey",
			"POST",
			json={"bucketId": bucket_id, "accessKeyId": access_key_id, "permissions": READ_WRITE},
		)
