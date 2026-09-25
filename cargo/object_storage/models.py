from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class BucketCredentials:
	"""One key that opens a bucket."""

	access_key: str
	secret_access_key: str

	def asdict(self) -> dict[str, Any]:
		return asdict(self)


@dataclass
class CreateBucketResponse:
	"""A bucket and the key to it."""

	name: str
	region: str
	credentials: BucketCredentials

	def asdict(self) -> dict[str, Any]:
		return asdict(self)


@dataclass
class DeleteBucketResponse:
	"""The bucket that went."""

	name: str
	region: str

	def asdict(self) -> dict[str, Any]:
		return asdict(self)


@dataclass
class RemoveKeyResponse:
	"""The key that went. The bucket's other keys still open it."""

	name: str
	region: str
	access_key: str

	def asdict(self) -> dict[str, Any]:
		return asdict(self)


@dataclass
class BucketUsage:
	"""What a bucket holds now, against its caps. A None cap means uncapped."""

	used_bytes: int
	object_count: int
	quota_bytes: int | None
	quota_objects: int | None


@dataclass
class BucketUsageResponse:
	"""One bucket's usage."""

	name: str
	region: str
	usage: BucketUsage

	def asdict(self) -> dict[str, Any]:
		return asdict(self)


@dataclass
class SetQuotaResponse:
	"""The cap now on the bucket."""

	name: str
	region: str
	size_gib: int

	def asdict(self) -> dict[str, Any]:
		return asdict(self)


@dataclass
class RotateCredentialsResponse:
	"""The bucket's new key. The one it replaces, if any, opens nothing."""

	name: str
	region: str
	credentials: BucketCredentials

	def asdict(self) -> dict[str, Any]:
		return asdict(self)
