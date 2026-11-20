from __future__ import annotations

import typing
from functools import cached_property
from typing import Any

import requests
from urllib3.exceptions import ConnectTimeoutError

from cargo.atlas_client import host_port

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

API_PREFIX = "/v2/"
READ_WRITE = {"read": True, "write": True, "owner": False}


class Error(RuntimeError):
	"""A Garage Admin API call failed. One argument, so it survives a pickle round trip:
	that is how the pipeline engine carries an exception back to the flow that raised it."""


class Client:
	"""Garage's Admin API v2, for one cluster. Setting it up, judging its health and working
	its buckets all subclass this. Object traffic goes to the S3 endpoint, not here."""

	timeout: float = 30

	def __init__(self, cluster: ObjectStorageCluster, address: str | None = None) -> None:
		"""`address` asks that one node instead of the gateways."""
		self.cluster = cluster
		self.address = address

	@cached_property
	def urls(self) -> list[str]:
		"""Every node serves the same API, so any gateway answers for the cluster. Over the
		mesh: the admin API is never put behind the public proxy."""
		addresses = [self.address] if self.address else self.cluster.gateway_addresses
		return [f"http://{host_port(address, self.cluster.admin_port)}" for address in addresses]

	@cached_property
	def headers(self) -> dict[str, str]:
		return {"Authorization": f"Bearer {self.cluster.get_password('admin_token')}"}

	def call(self, endpoint: str, method: str = "GET", **kwargs: Any) -> Any:
		"""The next gateway is asked only when one could not be connected to, so a call that
		reached Garage is never sent twice."""
		for url in self.urls:
			try:
				response = requests.request(
					method,
					f"{url}{API_PREFIX}{endpoint}",
					headers=self.headers,
					timeout=self.timeout,
					**kwargs,
				)
				break
			except requests.ConnectionError as exception:
				if not is_never_connected(exception):
					raise Error(f"{endpoint}: {exception}") from exception
				error = exception
			except requests.RequestException as exception:
				raise Error(f"{endpoint}: {exception}") from exception
		else:
			raise Error(f"{endpoint}: {error}") from error

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

	def layout_history(self) -> dict[str, Any]:
		"""Each layout version and whether it is Current, Draining or Historical."""
		return self.call("GetClusterLayoutHistory")

	def assign_roles(self, roles: list[dict[str, Any]]) -> dict[str, Any]:
		"""Stage one role per node. Nothing takes effect until `apply_layout`."""
		return self.call("UpdateClusterLayout", "POST", json={"roles": roles})

	def apply_layout(self, version: int) -> dict[str, Any]:
		return self.call("ApplyClusterLayout", "POST", json={"version": version})

	def list_workers(self) -> dict[str, Any]:
		"""Every node's background workers under `success`, keyed by node id. A node that did
		not answer is under `error`."""
		return self.call("ListWorkers", "POST", params={"node": "*"}, json={})

	def skip_dead_nodes(self, version: int) -> dict[str, Any]:
		"""Mark the layout synced on nodes that are down. Refused when no quorum of the
		remaining nodes holds the data."""
		return self.call(
			"ClusterLayoutSkipDeadNodes", "POST", json={"version": version, "allowMissingData": False}
		)

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

	def delete_bucket(self, bucket_id: str) -> None:
		self.call("DeleteBucket", "POST", params={"id": bucket_id})

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

	def set_bucket_quota(
		self, bucket_id: str, max_size_bytes: int | None, max_objects: int | None = None
	) -> None:
		"""Cap the bucket's total object size. None lifts the cap."""
		self.call(
			"UpdateBucket",
			"POST",
			params={"id": bucket_id},
			json={"quotas": {"maxSize": max_size_bytes, "maxObjects": max_objects}},
		)

	def set_bucket_cors_rules(self, bucket_id: str, rules: list[dict[str, Any]]) -> None:
		"""Replace the bucket's CORS rules. Needs no owner key, unlike S3's PutBucketCors."""
		self.call("UpdateBucket", "POST", params={"id": bucket_id}, json={"corsRules": rules})


def is_never_connected(exception: requests.ConnectionError) -> bool:
	"""Whether the connection was refused or timed out, so Garage never saw the request.
	urllib3's `NewConnectionError` is a `ConnectTimeoutError` too."""
	reason = getattr(exception.args[0], "reason", None) if exception.args else None
	return isinstance(reason, ConnectTimeoutError)
