# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.image_builder.doctype.image_variant.image_variant import ADMIN_DOMAIN, ImageVariant
from cargo.testing import SETTINGS, use_test_settings

WILDCARD_DOMAIN = SETTINGS["wildcard_domain"]


class IntegrationTestImageVariant(IntegrationTestCase):
	"""What a flavour hands its kind's provision script."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		# A test that throws invalidates its rollback savepoint, so the row it inserted
		# outlives it. Every test gets its own release rather than the same one twice.
		self.version = f"v0.0.32-{frappe.generate_hash(length=6)}"
		self.image = frappe.get_doc({"doctype": "Image", "kind": "pilot", "version": self.version}).insert()

	def variant(self, site: str, frappe_version: str = "version-16") -> ImageVariant:
		variant: ImageVariant = frappe.get_doc(
			{
				"doctype": "Image Variant",
				"image": self.image.name,
				"frappe_version": frappe_version,
				"site": site,
			}
		).insert()
		variant.name_contents()
		variant.admin_password = "Pilot#Bench2026x"

		return variant

	def test_a_flavour_with_a_site_names_every_value_its_script_reads(self):
		environment = self.variant("Included").pilot_environment

		self.assertEqual(environment["VERSION"], self.version)
		self.assertEqual(environment["FRAPPE_VERSION"], "version-16")
		self.assertEqual(environment["ADMIN_DOMAIN"], ADMIN_DOMAIN)
		self.assertEqual(environment["WILDCARD_DOMAIN"], WILDCARD_DOMAIN)
		self.assertTrue(environment["SITE"])
		self.assertTrue(environment["BENCH"])

	def test_a_flavour_without_a_site_still_carries_the_domains(self):
		environment = self.variant("Not Included").pilot_environment

		self.assertEqual(environment["SITE"], "")
		self.assertEqual(environment["ADMIN_DOMAIN"], ADMIN_DOMAIN)
		self.assertEqual(environment["WILDCARD_DOMAIN"], WILDCARD_DOMAIN)

	def test_a_build_without_a_wildcard_domain_stops_before_it_rents_a_machine(self):
		frappe.db.set_single_value("Cargo Settings", "wildcard_domain", "")
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

		with patch("cargo.image_builder.builder.AtlasClient") as atlas:
			with self.assertRaises(frappe.ValidationError):
				self.variant("Included").build()

		atlas.from_settings.assert_not_called()
