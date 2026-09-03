from __future__ import annotations

import json
import typing
from typing import Any, Self

import boto3
import frappe
import requests
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from frappe import _

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

API_PREFIX = "/v2/"
READ_WRITE = {"read": True, "write": True, "owner": False}
S3_CONFIG = Config(signature_version="s3v4", s3={"addressing_style": "path"}, read_timeout=3600)


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

	def connect_nodes(self, identifiers: list[str]) -> list[dict[str, Any]]:
		"""Peer the nodes now, without restarting them. Gossip spreads from whoever answers."""
		connected = self.call("ConnectClusterNodes", "POST", json=identifiers)
		refused = [
			f"{identifier}: {result.get('error')}"
			for identifier, result in zip(identifiers, connected, strict=True)
			if not result.get("success")
		]
		if refused:
			raise GarageError(0, f"ConnectClusterNodes: {', '.join(refused)}")

		return connected

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


class MetadataBucket:
	"""Puts objects in one cluster's metadata bucket, and can reach nothing else: the bucket
	and the key that opens it both come from the cluster."""

	def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str) -> None:
		self.bucket = bucket
		self.s3 = boto3.client(
			"s3",
			endpoint_url=endpoint,
			aws_access_key_id=access_key,
			aws_secret_access_key=secret_key,
			region_name=region,
			config=S3_CONFIG,
		)

	@classmethod
	def for_cluster(cls, cluster: ObjectStorageCluster) -> Self:
		"""Refuses a cluster whose bucket setup has not run: there is nothing to write to."""
		secret = cluster.get_password("metadata_bucket_secret_key", raise_exception=False)
		if not (cluster.metadata_bucket and cluster.metadata_bucket_access_key and secret):
			frappe.throw(_(f"{cluster.name} has no metadata bucket to upload to."))

		return cls(
			endpoint=f"http://{cluster.gateway_address()}:{cluster.s3_port}",
			bucket=cluster.metadata_bucket,
			access_key=cluster.metadata_bucket_access_key,
			secret_key=secret,
			region=cluster.region,
		)

	def upload(self, key: str, path: str) -> str:
		"""Put a file in the bucket under `key`. Large files go up in parts on their own."""
		try:
			self.s3.upload_file(path, self.bucket, key)
		except (ClientError, BotoCoreError) as exception:
			raise GarageError(0, f"PUT {self.bucket}/{key}: {exception}") from exception

		return key

	def write(self, key: str, document: dict[str, Any]) -> str:
		"""Put a small JSON document in the bucket under `key`."""
		try:
			self.s3.put_object(
				Bucket=self.bucket,
				Key=key,
				Body=json.dumps(document, indent=1).encode(),
				ContentType="application/json",
			)
		except (ClientError, BotoCoreError) as exception:
			raise GarageError(0, f"PUT {self.bucket}/{key}: {exception}") from exception

		return key

	def delete(self, key: str) -> None:
		"""Remove an object. S3 deletes are idempotent: a missing key is not an error."""
		try:
			self.s3.delete_object(Bucket=self.bucket, Key=key)
		except (ClientError, BotoCoreError) as exception:
			raise GarageError(0, f"DELETE {self.bucket}/{key}: {exception}") from exception
