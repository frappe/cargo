# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

import frappe
from frappe.integrations.doctype.webhook.webhook import get_webhook_data
from frappe.tests import IntegrationTestCase
from frappe.utils.password import remove_encrypted_password

from cargo.telemetry.doctype.datum_server.datum_server import PUBLIC_KEY_FILE, WEBHOOK_ENDPOINT
from cargo.testing import SETTINGS, use_test_settings

PEM = "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQ\n-----END PUBLIC KEY-----"


class IntegrationTestDatumServer(IntegrationTestCase):
	"""The environment a datum host runs on, and the two ways it can check tokens."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()

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

	def test_a_key_of_whitespace_is_no_key_at_all(self):
		"""Whitespace is truthy, so it would satisfy the either/or and then advertise a key
		file holding nothing -- the 401-to-everything this refuses."""
		self.assertFalse(self.saved(public_key="   \n  "))

	def test_a_key_is_stored_stripped(self):
		"""It is written to the host verbatim, so the padding would go with it."""
		doc = self.server(public_key=f"  {PEM}  \n")
		doc.insert()
		self.addCleanup(frappe.db.rollback)

		self.assertEqual(doc.public_key, PEM)

	def test_a_port_outside_the_range_is_refused(self):
		"""It reaches datum as a string it cannot argue with: the host starts, then cannot
		reach ClickHouse."""
		issuer = "https://central.test"

		self.assertFalse(self.saved(oidc_issuer=issuer, clickhouse_port=0))
		self.assertFalse(self.saved(oidc_issuer=issuer, clickhouse_port=65536))
		self.assertTrue(self.saved(oidc_issuer=issuer, clickhouse_port=65535))

	def test_a_timeout_of_zero_is_refused(self):
		"""Zero is not "no timeout": every ClickHouse call would expire at once."""
		issuer = "https://central.test"

		self.assertFalse(self.saved(oidc_issuer=issuer, timeout_seconds=0))
		self.assertFalse(self.saved(oidc_issuer=issuer, timeout_seconds=-5))

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


class IntegrationTestTelemetryWebhook(IntegrationTestCase):
	"""Central hands pilots this host's URL, so it has to hear when the host settles."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		self.addCleanup(frappe.db.rollback)

	def insert_server(self):
		return frappe.get_doc(
			{
				"doctype": "Datum Server",
				"clickhouse_host": "clickhouse.internal",
				"repository": "https://github.com/frappe/datum",
				"version": "develop",
				"public_key": PEM,
			}
		).insert()

	def webhook_of(self, server):
		return frappe.get_doc("Webhook", f"datum_server-{server.name}")

	def test_a_new_host_gets_a_webhook_pointed_at_central(self):
		webhook = self.webhook_of(self.insert_server())

		self.assertEqual(webhook.webhook_doctype, "Datum Server")
		self.assertTrue(webhook.request_url.endswith(WEBHOOK_ENDPOINT))
		self.assertTrue(webhook.enable_security)

	def test_only_a_settled_host_is_reported(self):
		condition = self.webhook_of(self.insert_server()).condition

		self.assertTrue(frappe.safe_eval(condition, eval_locals={"doc": frappe._dict(status="Active")}))
		self.assertTrue(frappe.safe_eval(condition, eval_locals={"doc": frappe._dict(status="Failed")}))
		self.assertFalse(frappe.safe_eval(condition, eval_locals={"doc": frappe._dict(status="Setting Up")}))

	def test_the_report_names_the_region_as_telemetry(self):
		server = self.insert_server()
		server.db_set("status", "Active")
		server.reload()

		report = get_webhook_data(server, self.webhook_of(server))

		self.assertEqual(report["region"], SETTINGS["region"])
		self.assertEqual(report["service"], "telemetry")
		self.assertEqual(report["status"], "Active")

	def test_a_cargo_with_no_webhook_secret_makes_no_host(self):
		"""Nothing may post to Central unauthenticated, so the host does not get made."""
		remove_encrypted_password("Cargo Settings", "Cargo Settings", "central_webhook_secret")
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

		with self.assertRaises(frappe.ValidationError):
			self.insert_server()
