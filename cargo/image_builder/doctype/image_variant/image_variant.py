# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from cargo.image_builder.builder import Builder


class ImageVariant(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		built_at: DF.Datetime | None
		checksum: DF.Data | None
		error: DF.LongText | None
		frappe_version: DF.Literal["version-15", "version-16", "develop"]
		image: DF.Link
		object_key: DF.Data | None
		site: DF.Literal["Included", "Not Included"]
		size_bytes: DF.Int
		status: DF.Literal["Draft", "Building", "Available", "Failed"]
	# end: auto-generated types

	"""One flavour of a release's image, and the artifact it produced.

	Each variant builds, fails and retries on its own, so a broken develop build leaves the
	version-16 image alone."""

	def validate(self) -> None:
		"""A dimension is blank, never None: the two would otherwise be different rows to a
		query, and the same flavour could be created twice."""
		self.frappe_version = self.frappe_version or ""
		self.site = self.site or ""

	def before_naming(self) -> None:
		"""The flavour is the name, so the same combination cannot be created twice. A
		dimension left blank does not apply to this image and drops out."""
		self.flavour = "-".join(part for part in (self.image, self.frappe_version, self.site) if part)

	@property
	def builder(self) -> Builder:
		return Builder(self.image, self.frappe_version, self.site)

	@frappe.whitelist()
	def build(self) -> None:
		"""Build this flavour and upload it, unless the bucket already has it."""
		...

	def is_uploaded(self) -> bool:
		"""Whether the metadata bucket already holds this flavour."""
		...

	def upload(self, path: str) -> None:
		"""Put the built image in the metadata bucket."""
		...

	def get_download_url(self) -> str:
		"""Where Atlas pulls this flavour from."""
		...
