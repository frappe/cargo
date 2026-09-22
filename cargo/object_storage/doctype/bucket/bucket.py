# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.synchronization import filelock

from cargo.object_storage.client import Client, Error
from cargo.object_storage.models import BucketCredentials, BucketUsage


class Bucket(Document):
	"""One bucket on this region's cluster, and the single key that opens it. Object traffic
	never comes here: a bench speaks S3 to the gateway."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		access_key: DF.Data | None
		bucket_name: DF.Data
		cluster: DF.Link
		max_objects: DF.Int
		max_size_gib: DF.Int
		secret_access_key: DF.Password | None
	# end: auto-generated types

	@property
	def garage(self) -> Client:
		return Client(frappe.get_cached_doc("Object Storage Cluster", self.cluster))

	@property
	def key_name(self) -> str:
		"""One key to a bucket, named after it, so a caller holding neither can find it."""
		return f"{self.bucket_name}-key"

	def lock(self):
		"""One caller at a time per bucket name."""
		return filelock(f"garage-bucket-{self.bucket_name}", timeout=30)

	def before_save(self) -> None:
		"""Since passwords are not nicely handled in after insert."""
		if self.is_new():
			self.provision()

	def on_update(self) -> None:
		# An uncapped new bucket needs no call: Garage caps nothing until it is told to.
		if self.flags.in_insert and not (self.max_size_gib or self.max_objects):
			return

		if self.has_value_changed("max_size_gib") or self.has_value_changed("max_objects"):
			self.apply_quota()

	def on_trash(self) -> None:
		"""Drop the bucket and its key. In case deletion is not possible raise."""
		with self.lock():
			bucket_id = self.get_bucket_id()
			if not bucket_id:
				return

			# The bucket first: a refused delete would otherwise leave it live with its key
			# already gone, reachable by nobody. A key outliving its bucket opens nothing.
			self.garage.delete_bucket(bucket_id)
			self.drop_key()

	def provision(self) -> None:
		"""Ensure a bucket with a key is created, if the key fails the bucket is deleted."""
		with self.lock():
			bucket_id = self.add_bucket()
			try:
				credentials = self.issue_credentials(bucket_id)
			except Exception:
				self.garage.delete_bucket(bucket_id)
				raise

		self.access_key = credentials.access_key
		self.secret_access_key = credentials.secret_access_key

	def get_bucket_id(self) -> str | None:
		"""The bucket behind this name, or None when nothing answers to it."""
		found = self.garage.bucket(self.bucket_name)

		return found["id"] if found else None

	def add_bucket(self) -> str:
		"""Create the bucket and name it. Named last, so an abandoned one squats no name."""
		bucket_id = self.garage.create_bucket()["id"]
		try:
			self.garage.add_bucket_alias(bucket_id, self.bucket_name)
		except Error as error:
			self.garage.delete_bucket(bucket_id)
			if "already exists" in str(error):
				frappe.throw(
					_("The bucket name {0} is already taken. Pick another.").format(self.bucket_name),
					title=_("Bucket name taken"),
				)
			if "InvalidBucketName" in str(error):
				frappe.throw(
					_(
						"{0} is not a valid bucket name. Use 3 to 63 characters: lowercase"
						" letters, digits, dots and hyphens."
					).format(self.bucket_name),
					title=_("Invalid bucket name"),
				)

			raise

		return bucket_id

	def issue_credentials(self, bucket_id: str | None = None) -> BucketCredentials:
		"""An S3 key for this bucket and nothing else, handed back once. Takes the bucket it
		belongs to rather than reading the name again, and no lock: it runs inside one."""
		bucket_id = bucket_id or self.get_bucket_id()
		if not bucket_id:
			frappe.throw(_("This cluster has no bucket called {0}.").format(self.bucket_name))

		key = self.garage.create_key(self.key_name)
		try:
			self.garage.allow_bucket_key(bucket_id, key["accessKeyId"])
		except Error:
			# A key that cannot be granted reaches nothing and is tracked by nothing.
			self.garage.delete_key(key["accessKeyId"])
			raise

		return BucketCredentials(access_key=key["accessKeyId"], secret_access_key=key["secretAccessKey"])

	@frappe.whitelist()
	def rotate_credentials(self) -> BucketCredentials:
		"""Replace this bucket's key and record it."""
		credentials = self.rotate_key()
		self.save()

		return credentials

	def rotate_key(self) -> BucketCredentials:
		"""Replace this bucket's key in Garage and put it on the record, unsaved. Minted
		before the old one goes, so a rotation that fails halfway leaves the bucket
		reachable rather than shut."""
		with self.lock():
			# Read first: the new key is created under the same name.
			previous = self.garage.key(self.key_name)
			credentials = self.issue_credentials()
			if previous:
				self.garage.delete_key(previous["accessKeyId"])

		self.access_key = credentials.access_key
		self.secret_access_key = credentials.secret_access_key

		return credentials

	@frappe.whitelist()
	def revoke_credentials(self) -> None:
		"""Take this bucket's key out of service. The bucket and its objects are untouched."""
		with self.lock():
			self.drop_key()

		self.access_key = None
		self.secret_access_key = None
		self.save()

	@frappe.whitelist()
	def get_usage(self) -> BucketUsage:
		"""What the bucket holds, and its caps. Garage counts as it writes -- it needs the
		running total to enforce a quota -- so this reads metadata and scans nothing."""
		info = self.garage.bucket(self.bucket_name)
		if not info:
			frappe.throw(_("This cluster has no bucket called {0}.").format(self.bucket_name))

		quotas = info.get("quotas") or {}

		return BucketUsage(
			used_bytes=info["bytes"],
			object_count=info["objects"],
			quota_bytes=quotas.get("maxSize"),
			quota_objects=quotas.get("maxObjects"),
		)

	def apply_quota(self) -> None:
		"""Send this record's caps to Garage, which counts bytes as it writes them. Zero is
		how the form says uncapped, and Garage lifts a cap with a null."""
		with self.lock():
			bucket_id = self.get_bucket_id()
			if not bucket_id:
				frappe.throw(_("This cluster has no bucket called {0}.").format(self.bucket_name))

			self.garage.set_bucket_quota(
				bucket_id,
				self.max_size_gib * 1024**3 if self.max_size_gib else None,
				self.max_objects or None,
			)

	def drop_key(self) -> None:
		"""Delete this bucket's key if Garage still holds one. No lock: it runs inside one."""
		if key := self.garage.key(self.key_name):
			self.garage.delete_key(key["accessKeyId"])
