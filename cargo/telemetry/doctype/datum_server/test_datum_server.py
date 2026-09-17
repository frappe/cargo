# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import Mock, patch

import frappe
from frappe.integrations.doctype.webhook.webhook import get_webhook_data, get_webhook_headers
from frappe.tests import IntegrationTestCase
from frappe.utils.password import remove_encrypted_password

from cargo.client_models import TELEMETRY
from cargo.proxy_client import ProxyClient, ProxyError
from cargo.telemetry.doctype.datum_server.datum_server import (
	DATUM_PORT,
	PUBLIC_KEY_FILE,
	SENDER,
	SENDER_HEADER,
	WEBHOOK_ENDPOINT,
	WEBHOOK_NAME,
	DatumServer,
)
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
		"""Each attempt is rolled back, which takes the test settings with it, so every
		attempt lays them down again rather than leaning on whatever the site has committed."""
		use_test_settings()
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

	def test_the_datum_user_password_is_decrypted(self):
		environment = self.environment(oidc_issuer="https://central.test", datum_user_password="ch")

		self.assertEqual(environment["DATUM_USER_PASSWORD"], "ch")

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

	def test_every_user_password_is_generated_and_distinct(self):
		"""The install writes all three into ClickHouse, so nothing has to be kept in step."""
		doc = self.server(oidc_issuer="https://central.test")
		doc.insert()
		self.addCleanup(frappe.db.rollback)

		secrets = {
			field: doc.get_password(field)
			for field in ("datum_user_password", "insights_user_password", "default_user_password")
		}

		self.assertTrue(all(secrets.values()))
		self.assertEqual(len(set(secrets.values())), 3)
		# datum-migrate refuses a password carrying `--` or a quote.
		self.assertTrue(all(value.isalnum() for value in secrets.values()))

	def test_a_host_saved_rather_than_inserted_still_gets_its_secrets(self):
		"""A Single takes the update path; a `before_insert` would leave empty passwords."""
		self.addCleanup(frappe.db.rollback)
		server = frappe.get_single("Datum Server")
		server.update(
			{
				"clickhouse_host": "clickhouse.internal",
				"repository": "https://github.com/frappe/datum",
				"version": "develop",
				"public_key": PEM,
			}
		).save()

		for field in ("datum_user_password", "insights_user_password", "default_user_password"):
			with self.subTest(field=field):
				self.assertTrue(server.get_password(field))

	def test_the_two_clickhouse_users_get_their_own_passwords(self):
		"""Nobody types these: datum-migrate creates both users with what is generated here."""
		doc = self.server(oidc_issuer="https://central.test")
		doc.insert()
		self.addCleanup(frappe.db.rollback)

		datum = doc.get_password("datum_user_password")
		insights = doc.get_password("insights_user_password")

		self.assertTrue(datum and insights)
		self.assertNotEqual(datum, insights)

	def test_the_api_connects_as_datum_not_the_superuser(self):
		"""Datum defaults to ClickHouse's `default` user, which may write anywhere."""
		self.assertEqual(
			self.environment(oidc_issuer="https://central.test")["DATUM_CLICKHOUSE_USER"], "datum"
		)

	def test_the_admin_password_is_handed_to_the_migration(self):
		"""Only datum-migrate uses it, to create the other two users."""
		self.assertTrue(self.environment(oidc_issuer="https://central.test")["DEFAULT_USER_PASSWORD"])


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

	def webhook(self):
		return frappe.get_doc("Webhook", WEBHOOK_NAME)

	def test_a_new_host_gets_a_webhook_pointed_at_central(self):
		self.insert_server()
		webhook = self.webhook()

		self.assertEqual(webhook.webhook_doctype, "Datum Server")
		self.assertTrue(webhook.request_url.endswith(WEBHOOK_ENDPOINT))
		self.assertTrue(webhook.enable_security)

	def test_a_host_saved_rather_than_inserted_still_gets_its_webhook(self):
		"""A Single takes the update path, where an `after_insert` would never run."""
		frappe.get_single("Datum Server").update(
			{
				"clickhouse_host": "clickhouse.internal",
				"repository": "https://github.com/frappe/datum",
				"version": "develop",
				"public_key": PEM,
			}
		).save()

		self.assertTrue(frappe.db.exists("Webhook", WEBHOOK_NAME))

	def test_the_blank_host_installing_the_app_leaves_reports_nothing(self):
		"""`init_singles` writes it blank at install, before Cargo Settings has a Central."""
		frappe.db.set_single_value("Cargo Settings", "central_url", "")
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

		blank = frappe.new_doc("Datum Server")
		blank.flags.ignore_mandatory = True
		blank.flags.ignore_validate = True
		blank.save()

		self.assertFalse(frappe.db.exists("Webhook", WEBHOOK_NAME))

	def test_only_a_settled_host_is_reported(self):
		self.insert_server()
		condition = self.webhook().condition

		self.assertTrue(frappe.safe_eval(condition, eval_locals={"doc": frappe._dict(status="Active")}))
		self.assertTrue(frappe.safe_eval(condition, eval_locals={"doc": frappe._dict(status="Failed")}))
		self.assertFalse(frappe.safe_eval(condition, eval_locals={"doc": frappe._dict(status="Setting Up")}))

	def test_the_report_names_the_region_as_telemetry(self):
		server = self.insert_server()
		server.db_set("status", "Active")
		server.reload()

		report = get_webhook_data(server, self.webhook())

		self.assertEqual(report["region"], SETTINGS["region"])
		self.assertEqual(report["service"], "telemetry")
		self.assertEqual(report["status"], "Available")
		self.assertEqual(report["service_endpoint"], f"https://telemetry-svc.{SETTINGS['wildcard_domain']}")

	def test_the_delivery_names_cargo_as_its_sender(self):
		"""Central serves one endpoint for every plane, and routes on this header."""
		server = self.insert_server()

		self.assertEqual(get_webhook_headers(server, self.webhook())[SENDER_HEADER], SENDER)

	def test_a_failed_host_reports_itself_unavailable(self):
		server = self.insert_server()
		server.db_set("status", "Failed")
		server.reload()

		report = get_webhook_data(server, self.webhook())

		self.assertEqual(report["status"], "Not Available")

	def test_a_cargo_with_no_webhook_secret_will_not_save_the_host(self):
		"""Nothing may post to Central unauthenticated, so the host is refused."""
		remove_encrypted_password("Cargo Settings", "Cargo Settings", "central_webhook_secret")
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

		with self.assertRaises(frappe.ValidationError):
			self.insert_server()


class IntegrationTestTelemetryRouting(IntegrationTestCase):
	"""A host is Active only once the region's telemetry domain points at it."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		self.addCleanup(frappe.db.rollback)
		self.server = frappe.get_doc(
			{
				"doctype": "Datum Server",
				"clickhouse_host": "clickhouse.internal",
				"repository": "https://github.com/frappe/datum",
				"version": "develop",
				"public_key": PEM,
			}
		).insert()
		self.machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": self.server.doctype,
				"reference_name": self.server.name,
				"role": TELEMETRY,
				"disk_size_gb": 20,
				"vm_id": f"vm-{frappe.generate_hash(length=8)}",
				"address": "fdaa:1::9",
				"status": "Running",
			}
		).insert()
		self.server.db_set("machine", self.machine.name)
		self.server.reload()

	def proxy(self, **behaviour):
		client = Mock(**behaviour)

		return patch.object(ProxyClient, "from_settings", return_value=client), client

	def test_a_proxy_that_refuses_leaves_the_reason_on_the_host(self):
		patched, _client = self.proxy(map_domain=Mock(side_effect=ProxyError("proxy unreachable")))
		with patched:
			self.assertFalse(self.server.publish_proxy_routes())

		self.server.reload()
		self.assertEqual(self.server.status, "Failed")
		self.assertIn("proxy unreachable", self.server.error)

	def test_a_region_with_no_domain_publishes_nothing(self):
		frappe.db.set_single_value("Cargo Settings", "wildcard_domain", "")
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")
		patched, client = self.proxy()

		with patched:
			self.assertFalse(self.server.publish_proxy_routes())

		client.map_domain.assert_not_called()
		self.server.reload()
		self.assertEqual(self.server.status, "Failed")

	def test_both_domains_are_pointed_at_this_host(self):
		patched, client = self.proxy()
		with patched:
			self.assertTrue(self.server.publish_proxy_routes())

		domain = SETTINGS["wildcard_domain"]
		self.assertEqual(
			[call.args for call in client.map_domain.call_args_list],
			[
				(f"telemetry-svc.{domain}", self.machine.address),
				(f"telemetry-read-svc.{domain}", self.machine.address),
			],
		)

	def steps(self, installed=True, routed=True, published=True):
		return (
			patch.object(DatumServer, "start_setup_on_machine", return_value=installed),
			patch.object(DatumServer, "configure_routing", return_value=routed),
			patch.object(DatumServer, "publish_proxy_routes", return_value=published),
		)

	def test_an_install_that_failed_is_never_routed_to(self):
		install, routing, publishing = self.steps(installed=False)
		with install, routing as configured, publishing as published:
			self.server._setup()

		configured.assert_not_called()
		published.assert_not_called()

	def test_a_host_nginx_would_not_serve_is_never_published(self):
		install, routing, publishing = self.steps(routed=False)
		with install, routing, publishing as published:
			self.server._setup()

		published.assert_not_called()
		self.server.reload()
		self.assertNotEqual(self.server.status, "Active")

	def test_a_host_is_active_once_every_step_lands(self):
		install, routing, publishing = self.steps()
		with install, routing, publishing:
			self.server._setup()

		self.server.reload()
		self.assertEqual(self.server.status, "Active")

	def test_a_host_whose_routes_failed_is_not_active(self):
		install, routing, publishing = self.steps(published=False)
		with install, routing, publishing:
			self.server._setup()

		self.server.reload()
		self.assertNotEqual(self.server.status, "Active")

	def test_nginx_is_told_both_ports_and_who_may_name_the_client(self):
		environment = self.server.nginx_environment()

		self.assertEqual(environment["DATUM_PORT"], DATUM_PORT)
		self.assertEqual(environment["CLICKHOUSE_PORT"], self.server.clickhouse_port)
		self.assertEqual(environment["WILDCARD_DOMAIN"], SETTINGS["wildcard_domain"])
		self.assertIn("fd00::/8", environment["TRUSTED_PROXIES"])

	def test_a_region_with_no_domain_is_never_routed(self):
		frappe.db.set_single_value("Cargo Settings", "wildcard_domain", "")
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

		with self.assertRaisesRegex(frappe.ValidationError, "Wildcard Domain"):
			self.server.nginx_environment()

	def test_the_nginx_script_is_the_one_run(self):
		with patch("cargo.telemetry.doctype.datum_server.datum_server.run_over_ssh") as ran:
			self.assertTrue(self.server.configure_routing())

		sent = ran.call_args.args[1]
		self.assertIn("server_name telemetry-svc.${WILDCARD_DOMAIN}", sent)
		self.assertIn("server_name telemetry-read-svc.${WILDCARD_DOMAIN}", sent)
		self.assertIn(f"export DATUM_PORT={DATUM_PORT}", sent)

	def test_a_host_nginx_refuses_says_so(self):
		with patch(
			"cargo.telemetry.doctype.datum_server.datum_server.run_over_ssh",
			side_effect=RuntimeError("nginx -t failed"),
		):
			self.assertFalse(self.server.configure_routing())

		self.server.reload()
		self.assertEqual(self.server.status, "Failed")
		self.assertIn("nginx", self.server.error)
