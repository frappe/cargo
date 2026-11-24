# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.synchronization import filelock

from cargo.object_storage import cors
from cargo.object_storage.client import Client, Error
from cargo.object_storage.doctype.bucket_usage_day.bucket_usage_day import record_peaks, utc_now
from cargo.object_storage.models import BucketCredentials, BucketUsage

if TYPE_CHECKING:
	from cargo.object_storage.doctype.bucket_credential.bucket_credential import BucketCredential


class Bucket(Document):
	"""One bucket on this region's cluster, and the keys that open it. Object traffic never
	comes here: a bench speaks S3 to the gateway."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from cargo.object_storage.doctype.bucket_allowed_origin.bucket_allowed_origin import (
			BucketAllowedOrigin,
		)
		from cargo.object_storage.doctype.bucket_credential.bucket_credential import BucketCredential
		from cargo.object_storage.doctype.bucket_tag.bucket_tag import BucketTag

		allowed_origins: DF.Table[BucketAllowedOrigin]
		bucket_credentials: DF.Table[BucketCredential]
		bucket_name: DF.Data
		cluster: DF.Link
		max_objects: DF.Int
		max_size_gib: DF.Int
		tags: DF.Table[BucketTag]
	# end: auto-generated types

	@property
	def garage(self) -> Client:
		return Client(frappe.get_cached_doc("Object Storage Cluster", self.cluster))

	def lock(self):
		"""One caller at a time per bucket name."""
		return filelock(f"garage-bucket-{self.bucket_name}", timeout=30)

	@property
	def origins(self) -> list[str]:
		return [row.origin for row in self.allowed_origins]

	def validate(self) -> None:
		"""Origins as the browser sends them, since Garage matches the Origin header exactly."""
		origins = cors.normalize_origins(self.origins)
		if origins != self.origins:
			self.set("allowed_origins", [{"origin": origin} for origin in origins])

	def before_insert(self) -> None:
		"""Before Frappe names the key rows, so their secrets can be saved."""
		self.provision()

	def on_update(self) -> None:
		if self.flags.in_insert:
			# A cluster on an older Garage cannot hold bucket CORS rules until it is upgraded.
			if cors.supported_by(self.garage):
				self.apply_cors_rules()
		elif self.origins_changed():
			cors.require_support(self.garage)
			self.apply_cors_rules()

		# An uncapped new bucket needs no call: Garage caps nothing until it is told to.
		if self.flags.in_insert and not (self.max_size_gib or self.max_objects):
			return

		if self.has_value_changed("max_size_gib") or self.has_value_changed("max_objects"):
			self.apply_quota()

	def on_trash(self) -> None:
		"""Drop the bucket and its keys. In case deletion is not possible raise. The bucket is
		sampled first, so its last day keeps a usage row for Central."""
		with self.lock():
			read_at = utc_now()
			info = self.garage.bucket(self.bucket_name)
			if not info:
				return

			bucket_id = info["id"]
			record_peaks({self.bucket_name: info.get("bytes") or 0}, read_at)

			# The bucket first: a refused delete would otherwise leave it live with its keys
			# already gone, reachable by nobody. A key outliving its bucket opens nothing.
			try:
				self.garage.delete_bucket(bucket_id)
			except Error as error:
				if "BucketNotEmpty" in str(error):
					frappe.throw(
						_("Bucket {0} still holds objects. Empty it before deleting it.").format(
							self.bucket_name
						),
						title=_("Bucket not empty"),
					)
				raise

			for credential in self.bucket_credentials:
				self.garage.delete_key(credential.access_key)

	def provision(self) -> None:
		"""Ensure a bucket with a key is created, if the key fails the bucket is deleted."""
		with self.lock():
			bucket_id = self.add_bucket()
			try:
				credentials = self.issue_credentials(bucket_id)
			except Exception:
				self.garage.delete_bucket(bucket_id)
				raise

		self.flags.provisioned = frappe._dict(bucket_id=bucket_id, access_key=credentials.access_key)
		self.append("bucket_credentials", credentials.asdict())

	def discard_provisioned(self) -> None:
		"""Undo provision() for an insert that failed after it."""
		provisioned = self.flags.provisioned
		if not provisioned:
			return

		try:
			self.garage.delete_bucket(provisioned.bucket_id)
			self.garage.delete_key(provisioned.access_key)
		except Error:
			frappe.log_error(title=f"Garage bucket {self.bucket_name} left behind after a failed insert")

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

		key = self.garage.create_key(f"{self.bucket_name}-key")
		try:
			self.garage.allow_bucket_key(bucket_id, key["accessKeyId"])
		except Error:
			self.garage.delete_key(key["accessKeyId"])
			raise

		return BucketCredentials(access_key=key["accessKeyId"], secret_access_key=key["secretAccessKey"])

	def add_key(self, ignore_permissions: bool = False) -> BucketCredentials:
		"""Issue one more key and record it."""
		with self.lock():
			credentials = self.issue_credentials()
			self.append("bucket_credentials", credentials.asdict())
			try:
				self.save(ignore_permissions=ignore_permissions)
			except Exception:
				# An unrecorded key can never be found to delete.
				self.garage.delete_key(credentials.access_key)
				raise

		return credentials

	def rotate_key(self, access_key: str, ignore_permissions: bool = False) -> BucketCredentials:
		"""Replace one of this bucket's keys and record it."""
		credential = self.get_credential(access_key)
		with self.lock():
			credentials = self.issue_credentials()
			credential.update(credentials.asdict())
			try:
				self.save(ignore_permissions=ignore_permissions)
				self.garage.delete_key(access_key)
			except Exception:
				# An unrecorded key can never be found to delete.
				self.garage.delete_key(credentials.access_key)
				raise

		return credentials

	def remove_key(self, access_key: str, ignore_permissions: bool = False) -> None:
		"""Delete one of this bucket's keys and record it."""
		credential = self.get_credential(access_key)
		with self.lock():
			info = self.garage.bucket(self.bucket_name)
			if not info:
				frappe.throw(_("This cluster has no bucket called {0}.").format(self.bucket_name))

			# Counted in Garage: the record is committed after the lock is released.
			if len(info["keys"]) == 1:
				frappe.throw(_("Bucket {0} must have at least one key.").format(self.bucket_name))

			self.remove(credential)
			self.save(ignore_permissions=ignore_permissions)
			self.garage.delete_key(access_key)

	@frappe.whitelist()
	def add_credentials(self) -> BucketCredentials:
		return self.add_key()

	@frappe.whitelist()
	def rotate_credentials(self, access_key: str) -> BucketCredentials:
		return self.rotate_key(access_key)

	@frappe.whitelist()
	def remove_credentials(self, access_key: str) -> None:
		self.remove_key(access_key)

	def get_credential(self, access_key: str) -> BucketCredential:
		"""This bucket's row for `access_key`. Refuses a key it does not hold."""
		for credential in self.bucket_credentials:
			if credential.access_key == access_key:
				return credential

		frappe.throw(_("Bucket {0} holds no key {1}.").format(self.bucket_name, access_key))

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

	def origins_changed(self) -> bool:
		before = self.get_doc_before_save()
		return not before or before.origins != self.origins

	def apply_cors_rules(self) -> None:
		"""Send this bucket's origins to Garage, the dashboard's among them. Garage applies them
		to the browser's preflight and to every answer after it."""
		bucket_id = self.get_bucket_id()
		if not bucket_id:
			frappe.throw(_("This cluster has no bucket called {0}.").format(self.bucket_name))

		self.garage.set_bucket_cors_rules(bucket_id, cors.rules_for(self.origins))

	def apply_quota(self) -> None:
		"""Send this record's caps to Garage, which counts bytes as it writes them. Zero is
		how the form says uncapped, and Garage lifts a cap with a null."""
		bucket_id = self.get_bucket_id()
		if not bucket_id:
			frappe.throw(_("This cluster has no bucket called {0}.").format(self.bucket_name))

		self.garage.set_bucket_quota(
			bucket_id,
			self.max_size_gib * 1024**3 if self.max_size_gib else None,
			self.max_objects or None,
		)
