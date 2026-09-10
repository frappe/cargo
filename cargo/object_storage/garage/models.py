from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class BucketCredentials:
	"""The one key that opens a bucket."""

	access_key: str
	secret_access_key: str


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
class RotateCredentialsResponse:
	"""The bucket's new key. The one it replaces opens nothing."""

	name: str
	region: str
	credentials: BucketCredentials

	def asdict(self) -> dict[str, Any]:
		return asdict(self)
