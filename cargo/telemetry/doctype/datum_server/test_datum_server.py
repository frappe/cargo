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
	REGION_HEADER,
	SOURCE,
	SOURCE_HEADER,
	WEBHOOK_NAME,
	DatumServer,
)
from cargo.testing import SETTINGS, use_test_settings


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
				"branch": "develop",
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

	def test_a_port_outside_the_range_is_refused(self):
		"""It reaches datum as a string it cannot argue with: the host starts, then cannot
		reach ClickHouse."""
		self.assertFalse(self.saved(clickhouse_port=0))
		self.assertFalse(self.saved(clickhouse_port=65536))
		self.assertTrue(self.saved(clickhouse_port=65535))

	def test_a_timeout_of_zero_is_refused(self):
		"""Zero is not "no timeout": every ClickHouse call would expire at once."""
		self.assertFalse(self.saved(timeout_seconds=0))
		self.assertFalse(self.saved(timeout_seconds=-5))

	def test_a_clickhouse_version_that_is_not_a_package_version_is_refused(self):
		"""It reaches apt as `clickhouse-server=<version>`, which only takes an exact one."""
		for version in ("latest", "26.3", "26.3.52.9; reboot"):
			with self.subTest(version=version):
				self.assertFalse(self.saved(clickhouse_version=version))

		self.assertTrue(self.saved(clickhouse_version="26.5.7.64"))

	def test_a_host_saved_before_the_version_existed_gets_the_default(self):
		self.addCleanup(frappe.db.rollback)
		server = self.server(clickhouse_version=None)
		server.save()

		self.assertEqual(server.clickhouse_version, "26.3.52.9")

	def saved_without(self, field: str, value) -> bool:
		"""Save a host with one Cargo Setting cleared. `saved` lays the test settings down
		first, so the clearing has to come after it, and the controller reads the cached
		document, so the cache goes with it."""
		use_test_settings()
		frappe.db.set_single_value("Cargo Settings", field, value)
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")
		self.addCleanup(frappe.clear_document_cache, "Cargo Settings", "Cargo Settings")
		try:
			self.server().save()
			return True
		except frappe.ValidationError:
			return False
		finally:
			frappe.db.rollback()

	def test_no_way_to_check_a_token_is_refused(self):
		"""Without a key set datum answers 401 to every call, so it is never set up."""
		self.assertFalse(self.saved_without("jwks_url", ""))

	def test_no_region_is_refused(self):
		"""Every region reads the same key set, so a datum without a region of its own
		would take any region's token."""
		self.assertFalse(self.saved_without("region_id", 0))

	def test_the_key_set_and_region_reach_datum_from_settings(self):
		"""Both are Cargo Settings' to own: datum verifies against the same merged set for
		the same region, so restating them on the host is a second copy that can drift."""
		environment = self.environment()

		self.assertEqual(environment["DATUM_JWKS_URL"], SETTINGS["jwks_url"])
		self.assertEqual(environment["DATUM_REGION_ID"], str(SETTINGS["region_id"]))

	def test_the_clickhouse_defaults_are_carried(self):
		environment = self.environment()

		self.assertEqual(environment["DATUM_CLICKHOUSE_HOST"], "clickhouse.internal")
		self.assertEqual(environment["DATUM_CLICKHOUSE_PORT"], "8123")
		self.assertEqual(environment["DATUM_CLICKHOUSE_USER"], "datum")
		self.assertEqual(environment["DATUM_TIMEOUT"], "30")

	def test_the_datum_user_password_is_decrypted(self):
		environment = self.environment(datum_user_password="ch")

		self.assertEqual(environment["DATUM_USER_PASSWORD"], "ch")

	def test_the_install_needs_which_datum_to_install(self):
		"""`environment` is what datum runs on; the repo and branch are how it got there."""
		doc = self.server()
		doc.save()
		self.addCleanup(frappe.db.rollback)
		install = doc.install_environment()

		self.assertEqual(install["DATUM_REPOSITORY"], "https://github.com/frappe/datum")
		self.assertEqual(install["DATUM_BRANCH"], "develop")
		self.assertNotIn("DATUM_COMMIT", install)
		self.assertEqual(install["CLICKHOUSE_VERSION"], "26.3.52.9")
		self.assertNotIn("DATUM_REPOSITORY", doc.environment())

	def test_nothing_unset_is_handed_to_datum(self):
		"""An empty variable is not the same as an unset one: datum treats "" as configured."""
		self.assertNotIn("DATUM_CLICKHOUSE_HOST", self.environment(clickhouse_host=""))

	def test_every_user_password_is_generated_and_distinct(self):
		"""The install writes all three into ClickHouse, so nothing has to be kept in step."""
		doc = self.server()
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
				"branch": "develop",
			}
		).save()

		for field in ("datum_user_password", "insights_user_password", "default_user_password"):
			with self.subTest(field=field):
				self.assertTrue(server.get_password(field))

	def test_the_two_clickhouse_users_get_their_own_passwords(self):
		"""Nobody types these: datum-migrate creates both users with what is generated here."""
		doc = self.server()
		doc.insert()
		self.addCleanup(frappe.db.rollback)

		datum = doc.get_password("datum_user_password")
		insights = doc.get_password("insights_user_password")

		self.assertTrue(datum and insights)
		self.assertNotEqual(datum, insights)

	def test_the_api_connects_as_datum_not_the_superuser(self):
		"""Datum defaults to ClickHouse's `default` user, which may write anywhere."""
		self.assertEqual(self.environment()["DATUM_CLICKHOUSE_USER"], "datum")

	def test_the_admin_password_is_handed_to_the_migration(self):
		"""Only datum-migrate uses it, to create the other two users."""
		self.assertTrue(self.environment()["DEFAULT_USER_PASSWORD"])

	def test_a_system_manager_can_see_each_user_password(self):
		doc = self.server()
		doc.insert()
		self.addCleanup(frappe.db.rollback)

		for field in ("datum_user_password", "insights_user_password", "default_user_password"):
			with self.subTest(field=field):
				self.assertEqual(doc.reveal_password(field), doc.get_password(field))

	def test_only_user_passwords_can_be_revealed(self):
		doc = self.server()
		doc.insert()
		self.addCleanup(frappe.db.rollback)

		with self.assertRaises(frappe.ValidationError):
			doc.reveal_password("clickhouse_host")

	def test_nobody_else_can_see_a_user_password(self):
		doc = self.server()
		doc.insert()
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")

		with self.assertRaises(frappe.PermissionError):
			doc.reveal_password("datum_user_password")


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
				"branch": "develop",
			}
		).insert()

	def webhook(self):
		return frappe.get_doc("Webhook", WEBHOOK_NAME)

	def test_a_new_host_gets_a_webhook_pointed_at_central(self):
		self.insert_server()
		webhook = self.webhook()

		self.assertEqual(webhook.webhook_doctype, "Datum Server")
		self.assertEqual(webhook.request_url, SETTINGS["central_webhook_url"])
		self.assertTrue(webhook.enable_security)

	def test_a_host_saved_rather_than_inserted_still_gets_its_webhook(self):
		"""A Single takes the update path, where an `after_insert` would never run."""
		frappe.get_single("Datum Server").update(
			{
				"clickhouse_host": "clickhouse.internal",
				"repository": "https://github.com/frappe/datum",
				"branch": "develop",
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

		headers = get_webhook_headers(server, self.webhook())

		self.assertEqual(headers[SOURCE_HEADER], SOURCE)
		self.assertEqual(headers[REGION_HEADER], SETTINGS["region"])

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
				"branch": "develop",
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
			patch.multiple(
				DatumServer,
				start_setup_on_machine=Mock(return_value=installed),
				get_datum_commit=Mock(return_value="c" * 40),
			),
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

	def test_setup_records_the_commit_it_installed(self):
		install, routing, publishing = self.steps()
		with install, routing, publishing:
			self.server._setup()

		self.server.reload()
		self.assertEqual(self.server.commit, "c" * 40)

	def test_setup_installs_the_pinned_commit(self):
		self.server.db_set("commit", "abc1234")
		self.server.reload()

		with patch("cargo.telemetry.doctype.datum_server.datum_server.run_over_ssh") as ran:
			self.server.start_setup_on_machine()

		self.assertIn("export DATUM_COMMIT=abc1234", ran.call_args.args[1])

	def test_a_blank_branch_falls_back_to_main(self):
		self.server.branch = ""
		self.server.save()

		self.assertEqual(self.server.branch, "main")

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


class IntegrationTestDatumUpdate(IntegrationTestCase):
	"""An update pulls datum on a host that is set up, and leaves it Active only if it answers."""

	setUp = IntegrationTestTelemetryRouting.setUp

	def ssh(self, **behaviour):
		return patch("cargo.telemetry.doctype.datum_server.datum_server.run_over_ssh", **behaviour)

	def test_a_host_that_is_not_set_up_cannot_be_updated(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Set datum up"):
			self.server.update_datum()

	def test_a_stopped_machine_cannot_be_updated(self):
		self.server.mark("Active")
		self.machine.db_set("status", "Stopped")

		with self.assertRaisesRegex(frappe.ValidationError, "Machine must be running"):
			self.server.update_datum()

	def test_an_update_runs_as_a_workflow_with_its_branch_and_commit(self):
		self.server.mark("Active")

		with patch.object(DatumServer, "_update") as update:
			self.server.update_datum(" main ", " abc1234 ")

		update.run_as_workflow.assert_called_once_with("main", "abc1234")
		self.assertEqual(self.server.status, "Updating")

	def test_an_update_without_a_branch_keeps_the_hosts(self):
		self.server.mark("Active")

		with patch.object(DatumServer, "_update") as update:
			self.server.update_datum()

		update.run_as_workflow.assert_called_once_with("develop", "")

	def test_the_update_script_is_given_the_commit(self):
		with self.ssh() as ran:
			self.assertTrue(self.server.update_on_machine("main", "abc1234"))

		sent = ran.call_args.args[1]
		self.assertIn("export DATUM_BRANCH=main", sent)
		self.assertIn("export DATUM_COMMIT=abc1234", sent)
		self.assertIn('TARGET="${DATUM_COMMIT:-$DATUM_BRANCH}"', sent)
		self.assertIn("systemctl restart datum", sent)

	def test_no_commit_takes_the_newest_of_the_branch(self):
		environment = self.server.install_environment("main", "")

		self.assertNotIn("DATUM_COMMIT", environment)
		self.assertEqual(environment["DATUM_BRANCH"], "main")

	UPDATED = "c" * 40

	def run_update(self, landed: bool):
		self.server.mark("Updating")
		with (
			patch.object(DatumServer, "update_on_machine", return_value=landed),
			patch.object(DatumServer, "get_datum_commit", return_value=self.UPDATED) as read,
		):
			self.server._update("main", "abc1234")

		self.server.reload()
		return read

	def test_an_update_that_lands_records_the_commit_datum_is_at(self):
		self.run_update(True)

		self.assertEqual(self.server.status, "Active")
		self.assertEqual(self.server.branch, "main")
		self.assertEqual(self.server.commit, self.UPDATED)

	def test_a_failed_update_leaves_the_host_to_the_health_ping(self):
		read = self.run_update(False)

		self.assertEqual(self.server.status, "Unreachable")
		self.assertEqual(self.server.branch, "develop")
		self.assertFalse(self.server.commit)
		read.assert_not_called()

	def test_a_failed_update_never_fails_the_host(self):
		with self.ssh(side_effect=RuntimeError("connection reset")):
			self.assertFalse(self.server.update_on_machine("main", "abc1234"))

		self.server.reload()
		self.assertNotEqual(self.server.status, "Failed")

	def test_an_update_whose_workflow_died_does_not_stay_updating(self):
		self.server.mark("Updating")
		self.server.on_workflow_failure(Mock())

		self.server.reload()
		self.assertEqual(self.server.status, "Unreachable")

	def test_a_setup_whose_workflow_died_is_failed_for_the_spawner_to_retry(self):
		self.server.mark("Setting Up")
		self.server.on_workflow_failure(Mock())

		self.server.reload()
		self.assertEqual(self.server.status, "Failed")

	def test_a_dead_workflow_leaves_a_settled_host_alone(self):
		self.server.mark("Active")
		self.server.on_workflow_failure(Mock())

		self.server.reload()
		self.assertEqual(self.server.status, "Active")

	def test_the_commit_datum_runs_at_is_read_from_the_host(self):
		with self.ssh(return_value=f"{self.UPDATED}\n") as ran:
			self.assertEqual(self.server.get_datum_commit(), self.UPDATED)

		self.assertIn("rev-parse HEAD", ran.call_args.args[1])
