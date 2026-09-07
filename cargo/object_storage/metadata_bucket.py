from __future__ import annotations

import json
import typing
from typing import Any, Self, TypedDict

import boto3
import frappe
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from frappe import _

from cargo.garage_admin_client import GarageError

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

S3_CONFIG = Config(signature_version="s3v4", s3={"addressing_style": "path"}, read_timeout=3600)


class MetadataBucketInfo(TypedDict):
	"""The metadata bucket and its credentials."""

	name: str
	access_key: str
	secret_key: str


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
			endpoint=f"http://{cluster.fleet.gateway_address}:{cluster.s3_port}",
			bucket=cluster.metadata_bucket,
			access_key=cluster.metadata_bucket_access_key,
			secret_key=secret,
			region=cluster.region,
		)

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
			raise GarageError(f"PUT {self.bucket}/{key}: {exception}") from exception

		return key
