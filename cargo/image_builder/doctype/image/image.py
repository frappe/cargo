# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

FRAPPE_VERSIONS = ("version-15", "version-16", "develop")
SITE_OPTIONS = ("Included", "Not Included")


class Image(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		pilot_version: DF.Data
	# end: auto-generated types

	"""One pilot release. Its flavours are Image Variants, one per built artifact."""

	@frappe.whitelist()
	def generate_variants(self) -> list[str]:
		"""Every Frappe version, with a site and without. Existing variants are left alone --
		the variant name is the flavour, so re-running this cannot duplicate one."""
		created = []
		for frappe_version in FRAPPE_VERSIONS:
			for site in SITE_OPTIONS:
				variant = frappe.get_doc(
					{
						"doctype": "Image Variant",
						"image": self.name,
						"frappe_version": frappe_version,
						"site": site,
					}
				)
				variant.autoname()
				if frappe.db.exists("Image Variant", variant.name):
					continue
				created.append(variant.insert().name)

		return created


def get_variant(pilot_version: str, frappe_version: str | None = None, site: str | None = None) -> str | None:
	"""The built image for one flavour, or None if it has not been built yet.

	A blank dimension matches only variants that left it blank -- a bare base image is not a
	Frappe image with the version forgotten. None and "" mean the same thing here; a Select
	stores "", so passing None unnormalised would query IS NULL and match nothing."""
	return frappe.db.get_value(
		"Image Variant",
		{
			"image": pilot_version,
			"frappe_version": frappe_version or "",
			"site": site or "",
			"status": "Available",
		},
	)
