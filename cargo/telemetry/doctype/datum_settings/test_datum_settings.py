# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

PEM = "-----BEGIN PUBLIC KEY-----\nMIIB\n-----END PUBLIC KEY-----"


class IntegrationTestDatumSettings(IntegrationTestCase):
	"""The environment a datum host runs on, and the two ways it can check tokens."""

	def setUp(self):
		frappe.set_user("Administrator")

	def settings(self, **changes):
		doc = frappe.get_single("Datum Settings")
		doc.update({"clickhouse_host": "clickhouse.internal", **changes})

		return doc

	def saved(self, **changes) -> bool:
		try:
			self.settings(**changes).save()
			return True
		except frappe.ValidationError:
			return False
		finally:
			frappe.db.rollback()

	def environment(self, **changes) -> dict:
		doc = self.settings(**changes)
		doc.save()
		self.addCleanup(frappe.db.rollback)

		return frappe.get_single("Datum Settings").environment()

	def test_no_way_to_check_a_token_is_refused(self):
		"""With neither, datum answers 401 to every call."""
		self.assertFalse(self.saved())

	def test_an_issuer_alone_is_enough(self):
		self.assertTrue(self.saved(oidc_issuer="https://central.test"))

	def test_a_public_key_alone_is_enough(self):
		self.assertTrue(self.saved(public_key=PEM))

	def test_a_trailing_slash_is_trimmed_from_the_issuer(self):
		"""Datum appends the discovery path, so a stray slash would double it."""
		self.settings(oidc_issuer="https://central.test/").save()
		self.addCleanup(frappe.db.rollback)

		self.assertEqual(frappe.get_single("Datum Settings").oidc_issuer, "https://central.test")

	def test_the_issuer_reaches_datum_as_its_own_variable(self):
		environment = self.environment(oidc_issuer="https://central.test")

		self.assertEqual(environment["DATUM_OIDC_ISSUER"], "https://central.test")
		self.assertNotIn("DATUM_JWT_PUBLIC_KEY_FILE", environment)

	def test_a_public_key_is_passed_as_a_file(self):
		"""Datum reads a path, not the PEM, so whoever writes it names the file."""
		environment = self.environment(public_key=PEM)

		self.assertEqual(environment["DATUM_JWT_PUBLIC_KEY_FILE"], "datum.pub")
		self.assertNotIn("DATUM_OIDC_ISSUER", environment)

	def test_the_clickhouse_defaults_are_carried(self):
		environment = self.environment(oidc_issuer="https://central.test")

		self.assertEqual(environment["DATUM_CLICKHOUSE_HOST"], "clickhouse.internal")
		self.assertEqual(environment["DATUM_CLICKHOUSE_PORT"], "8123")
		self.assertEqual(environment["DATUM_CLICKHOUSE_USER"], "datum")
		self.assertEqual(environment["DATUM_TIMEOUT"], "30")

	def test_the_clickhouse_password_is_decrypted(self):
		environment = self.environment(oidc_issuer="https://central.test", clickhouse_password="ch")

		self.assertEqual(environment["DATUM_CLICKHOUSE_PASSWORD"], "ch")

	def test_nothing_unset_is_handed_to_datum(self):
		"""An empty variable is not the same as an unset one: datum treats "" as configured."""
		self.assertNotIn("DATUM_CLICKHOUSE_PASSWORD", self.environment(oidc_issuer="https://central.test"))
