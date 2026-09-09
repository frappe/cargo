# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from cargo.telemetry.doctype.datum_server.datum_server import PUBLIC_KEY_FILE

PEM = "-----BEGIN PUBLIC KEY-----\nMIIB\n-----END PUBLIC KEY-----"


class IntegrationTestDatumServer(IntegrationTestCase):
	"""The environment a datum host runs on, and the two ways it can check tokens."""

	def setUp(self):
		frappe.set_user("Administrator")

	def server(self, **changes):
		doc = frappe.new_doc("Datum Server")
		doc.update(
			{
				"clickhouse_host": "clickhouse.internal",
				"repository": "https://github.com/frappe/datum",
				"version": "develop",
				**changes,
			}
		)

		return doc

	def saved(self, **changes) -> bool:
		try:
			self.server(**changes).save()
			return True
		except frappe.ValidationError:
			return False
		finally:
			frappe.db.rollback()

	def environment(self, **changes) -> dict:
		doc = self.server(**changes)
		doc.insert()
		self.addCleanup(frappe.db.rollback)

		return doc.environment()

	def test_no_way_to_check_a_token_is_refused(self):
		"""With neither, datum answers 401 to every call."""
		self.assertFalse(self.saved())

	def test_an_issuer_alone_is_enough(self):
		self.assertTrue(self.saved(oidc_issuer="https://central.test"))

	def test_a_public_key_alone_is_enough(self):
		self.assertTrue(self.saved(public_key=PEM))

	def test_a_trailing_slash_is_trimmed_from_the_issuer(self):
		"""Datum appends the discovery path, so a stray slash would double it."""
		doc = self.server(oidc_issuer="https://central.test/")
		doc.save()
		self.addCleanup(frappe.db.rollback)

		self.assertEqual(doc.oidc_issuer, "https://central.test")

	def test_the_issuer_reaches_datum_as_its_own_variable(self):
		environment = self.environment(oidc_issuer="https://central.test")

		self.assertEqual(environment["DATUM_OIDC_ISSUER"], "https://central.test")
		self.assertNotIn("DATUM_JWT_PUBLIC_KEY_FILE", environment)

	def test_a_public_key_is_passed_as_a_file_and_as_the_key(self):
		"""Datum reads a path, so the install is handed both: where to write it, and what."""
		environment = self.environment(public_key=PEM)

		self.assertEqual(environment["DATUM_JWT_PUBLIC_KEY_FILE"], PUBLIC_KEY_FILE)
		self.assertEqual(environment["DATUM_JWT_PUBLIC_KEY"], PEM)
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

	def test_the_install_needs_which_datum_to_install(self):
		"""`environment` is what datum runs on; the repo and version are how it got there."""
		doc = self.server(oidc_issuer="https://central.test")
		doc.save()
		self.addCleanup(frappe.db.rollback)
		install = doc.install_environment()

		self.assertEqual(install["DATUM_REPOSITORY"], "https://github.com/frappe/datum")
		self.assertEqual(install["DATUM_VERSION"], "develop")
		self.assertNotIn("DATUM_REPOSITORY", doc.environment())

	def test_nothing_unset_is_handed_to_datum(self):
		"""An empty variable is not the same as an unset one: datum treats "" as configured."""
		self.assertNotIn("DATUM_JWT_PUBLIC_KEY_FILE", self.environment(oidc_issuer="https://central.test"))

	def test_every_clickhouse_password_is_generated_and_distinct(self):
		"""The install writes all three into ClickHouse, so nothing has to be kept in step."""
		doc = self.server(oidc_issuer="https://central.test")
		doc.insert()
		self.addCleanup(frappe.db.rollback)

		secrets = {
			field: doc.get_password(field)
			for field in ("clickhouse_password", "insights_password", "default_password")
		}

		self.assertTrue(all(secrets.values()))
		self.assertEqual(len(set(secrets.values())), 3)
		# datum-migrate refuses a password carrying `--` or a quote.
		self.assertTrue(all(value.isalnum() for value in secrets.values()))

	def test_the_two_clickhouse_users_get_their_own_passwords(self):
		"""Nobody types these: datum-migrate creates both users with what is generated here."""
		doc = self.server(oidc_issuer="https://central.test")
		doc.insert()
		self.addCleanup(frappe.db.rollback)

		datum = doc.get_password("clickhouse_password")
		insights = doc.get_password("insights_password")

		self.assertTrue(datum and insights)
		self.assertNotEqual(datum, insights)

	def test_the_api_connects_as_datum_not_the_superuser(self):
		"""Datum defaults to ClickHouse's `default` user, which may write anywhere."""
		self.assertEqual(
			self.environment(oidc_issuer="https://central.test")["DATUM_CLICKHOUSE_USER"], "datum"
		)

	def test_the_admin_password_is_handed_to_the_migration(self):
		"""Only datum-migrate uses it, to create the other two users."""
		self.assertTrue(self.environment(oidc_issuer="https://central.test")["DATUM_DEFAULT_PASSWORD"])
