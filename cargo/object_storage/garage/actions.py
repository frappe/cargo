from __future__ import annotations

import frappe
from frappe import _
from frappe.utils.synchronization import filelock

from cargo.object_storage.garage.client import Client, Error
from cargo.object_storage.garage.models import BucketCredentials

ALIAS_TAKEN = "already exists"
LOCK_TIMEOUT = 30


class Actions(Client):
	"""Bucket work on one cluster, as Central and Atlas ask for it. Object traffic never
	comes here: a bench speaks S3 to the gateway."""

	def bucket_lock(self, alias: str):
		"""One caller at a time per bucket name. Garage arbitrates the name but not the key
		behind it, so two rotations at once would leave a live key nobody tracks."""
		return filelock(f"garage-bucket-{alias}", timeout=LOCK_TIMEOUT)

	def key_name(self, alias: str) -> str:
		"""One key to a bucket, named after it, so a caller holding neither can find it."""
		return f"{alias}-key"

	def bucket_id(self, alias: str) -> str | None:
		"""The bucket behind a name, or None when nothing answers to it."""
		found = self.bucket(alias)

		return found["id"] if found else None

	def add_bucket(self, alias: str) -> str:
		"""Create a bucket and name it. Named last, so an abandoned one squats no name."""
		bucket_id = self.create_bucket()["id"]
		try:
			self.add_bucket_alias(bucket_id, alias)
		except Error as error:
			self.delete_bucket(bucket_id)
			if ALIAS_TAKEN in str(error):
				frappe.throw(
					_("The bucket name {0} is already taken. Pick another.").format(alias),
					title=_("Bucket name taken"),
				)

			raise

		return bucket_id

	def provision_bucket(self, alias: str) -> BucketCredentials:
		"""A bucket and the key that opens it, or neither: one nobody holds a key to is
		unreachable and invisible."""
		with self.bucket_lock(alias):
			bucket_id = self.add_bucket(alias)
			try:
				return self.issue_credentials(alias, bucket_id)
			except Exception:
				self.delete_bucket(bucket_id)
				raise

	def remove_bucket(self, alias: str) -> None:
		"""Drop a bucket and its key. Garage answers 409 for a bucket that still holds
		objects, which is what keeps this from ever taking them with it."""
		with self.bucket_lock(alias):
			bucket_id = self.bucket_id(alias)
			if not bucket_id:
				return

			# The bucket first: a refused delete would otherwise leave it live with its key
			# already gone, reachable by nobody. A key outliving its bucket opens nothing.
			self.delete_bucket(bucket_id)
			if key := self.key(self.key_name(alias)):
				self.delete_key(key["accessKeyId"])

	def issue_credentials(self, alias: str, bucket_id: str | None = None) -> BucketCredentials:
		"""An S3 key for this bucket and nothing else, handed back once. Takes the bucket it
		belongs to rather than the name, and no lock: it runs inside one."""
		bucket_id = bucket_id or self.bucket_id(alias)
		if not bucket_id:
			frappe.throw(_("This cluster has no bucket called {0}.").format(alias))

		key = self.create_key(self.key_name(alias))
		try:
			self.allow_bucket_key(bucket_id, key["accessKeyId"])
		except Error:
			# A key that cannot be granted reaches nothing and is tracked by nothing.
			self.delete_key(key["accessKeyId"])
			raise

		return BucketCredentials(access_key=key["accessKeyId"], secret_access_key=key["secretAccessKey"])

	def rotate_credentials(self, alias: str) -> BucketCredentials:
		"""Replace this bucket's key. Minted before the old one goes, so a rotation that
		fails halfway leaves the bucket reachable rather than shut."""
		with self.bucket_lock(alias):
			# Read first: the new key is created under the same name.
			previous = self.key(self.key_name(alias))
			credentials = self.issue_credentials(alias)
			if previous:
				self.delete_key(previous["accessKeyId"])

			return credentials

	def revoke_credentials(self, alias: str) -> None:
		"""Take a bucket's key out of service. The bucket and its objects are untouched."""
		with self.bucket_lock(alias):
			if key := self.key(self.key_name(alias)):
				self.delete_key(key["accessKeyId"])
