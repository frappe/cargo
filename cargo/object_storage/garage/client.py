from __future__ import annotations

import typing
from functools import cached_property
from typing import Any

import requests

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

API_PREFIX = "/v2/"
READ_WRITE = {"read": True, "write": True, "owner": False}


class Error(RuntimeError):
	"""A Garage Admin API call failed. One argument, so it survives a pickle round trip:
	that is how the workflow engine carries an exception back to the flow that raised it."""


class Client:
	"""Garage's Admin API v2, for one cluster. Setting it up, judging its health and working
	its buckets all subclass this. Object traffic goes to the S3 endpoint, not here."""

	timeout: float = 30

	def __init__(self, cluster: ObjectStorageCluster) -> None:
		self.cluster = cluster

	@cached_property
	def url(self) -> str:
		"""Every node serves the same API, so the gateway answers for the cluster."""
		return f"http://{self.cluster.gateway_address}:{self.cluster.admin_port}"

	@cached_property
	def headers(self) -> dict[str, str]:
		return {"Authorization": f"Bearer {self.cluster.get_password('admin_token')}"}

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
			raise Error(f"{endpoint}: {exception}") from exception

		if response.status_code == 404:
			return None

		if not response.ok:
			raise Error(f"{endpoint} answered {response.status_code}: {response.text[:500]}")

		return response.json() if response.content else None

	def status(self) -> dict[str, Any]:
		return self.call("GetClusterStatus")

	def health(self) -> dict[str, Any]:
		"""Quorum and how many storage nodes are up, as Garage itself judges it."""
		return self.call("GetClusterHealth")

	def layout(self) -> dict[str, Any]:
		return self.call("GetClusterLayout")

	def assign_roles(self, roles: list[dict[str, Any]]) -> dict[str, Any]:
		"""Stage one role per node. Nothing takes effect until `apply_layout`."""
		return self.call("UpdateClusterLayout", "POST", json={"roles": roles})

	def apply_layout(self, version: int) -> dict[str, Any]:
		return self.call("ApplyClusterLayout", "POST", json={"version": version})

	def connect_nodes(self, identifiers: list[str]) -> list[dict[str, Any]]:
		"""Peer the nodes now, without restarting them. Gossip spreads from whoever answers."""
		connected = self.call("ConnectClusterNodes", "POST", json=identifiers)
		refused = [
			f"{identifier}: {result.get('error')}"
			for identifier, result in zip(identifiers, connected, strict=True)
			if not result.get("success")
		]
		if refused:
			raise Error(f"ConnectClusterNodes: {', '.join(refused)}")

		return connected

	def bucket(self, alias: str) -> dict[str, Any] | None:
		return self.call("GetBucketInfo", params={"globalAlias": alias})

	def create_bucket(self, alias: str | None = None) -> dict[str, Any]:
		"""A bucket, named now or left unnamed for `add_bucket_alias` to name later."""
		return self.call("CreateBucket", "POST", json={"globalAlias": alias} if alias else {})

	def add_bucket_alias(self, bucket_id: str, alias: str) -> dict[str, Any]:
		return self.call("AddBucketAlias", "POST", json={"bucketId": bucket_id, "globalAlias": alias})

	def remove_bucket_alias(self, bucket_id: str, alias: str) -> dict[str, Any]:
		return self.call("RemoveBucketAlias", "POST", json={"bucketId": bucket_id, "globalAlias": alias})

	def delete_bucket(self, bucket_id: str) -> None:
		self.call("DeleteBucket", "POST", params={"id": bucket_id})

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

	def delete_key(self, access_key_id: str) -> None:
		self.call("DeleteKey", "POST", params={"id": access_key_id})
