# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

import typing

import frappe
from frappe import _
from frappe.model.document import Document

if typing.TYPE_CHECKING:
	from cargo.image_builder.doctype.image_variant.image_variant import ImageVariant

FRAPPE_VERSIONS = ("version-15", "version-16", "develop")
SITE_OPTIONS = ("Included", "Not Included")


class Image(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		kind: DF.Literal["pilot"]
		version: DF.Data
	# end: auto-generated types

	"""One release of one kind of image. Its flavours are Image Variants, one per snapshot."""

	def validate(self) -> None:
		"""Kind and version are the identity. The name carries a counter, so nothing else
		stops the same release being registered twice."""
		if frappe.db.exists("Image", {"kind": self.kind, "version": self.version, "name": ("!=", self.name)}):
			frappe.throw(_("{0} {1} is already registered.").format(self.kind, self.version))

	@frappe.whitelist()
	def generate_variants(self) -> list[str]:
		"""Every Frappe version, with a site and without. Existing variants are left alone --
		the variant name is its flavour, so re-running this cannot duplicate one."""
		created = []
		for frappe_version in FRAPPE_VERSIONS:
			for site in SITE_OPTIONS:
				if frappe.db.exists(
					"Image Variant",
					{"image": self.name, "frappe_version": frappe_version, "site": site},
				):
					continue

				variant: ImageVariant = frappe.get_doc(
					{
						"doctype": "Image Variant",
						"image": self.name,
						"frappe_version": frappe_version,
						"site": site,
					}
				)
				created.append(variant.insert().name)

		return created
