# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class IntegrationTestCargoSettings(IntegrationTestCase):
	"""Every service builds its domains from these settings, so they hold one canonical form."""

	def test_the_wildcard_domain_is_stored_in_one_canonical_form(self) -> None:
		settings = frappe.get_doc("Cargo Settings")

		for written in (" .Example.COM. ", "example.com ", ".example.com", "EXAMPLE.com"):
			settings.wildcard_domain = written
			settings.validate()

			self.assertEqual(settings.wildcard_domain, "example.com", f"from {written!r}")

	def test_an_empty_wildcard_domain_stays_empty(self) -> None:
		settings = frappe.get_doc("Cargo Settings")
		settings.wildcard_domain = None
		settings.validate()

		self.assertEqual(settings.wildcard_domain, "")
