# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

import shlex
from unittest.mock import Mock, patch

import frappe
from frappe.integrations.doctype.webhook.webhook import get_webhook_data, get_webhook_headers
from frappe.tests import IntegrationTestCase
from frappe.utils.password import remove_encrypted_password

from cargo.client_models import TELEMETRY
from cargo.telemetry.doctype.datum_server.datum_server import (
	REGION_HEADER,
	SOURCE,
	SOURCE_HEADER,
	WEBHOOK_NAME,
	DatumServer,
)
from cargo.telemetry.doctype.datum_server.proxy import Proxy
from cargo.testing import SETTINGS, use_test_settings

PUBLIC_IPV4 = "203.0.113.20"


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
		self.assertEqual(
			report["service_endpoint"], f"http://internal-telemetry-svc.{SETTINGS['wildcard_domain']}"
		)

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
	"""A host is Active only once its public domains answer through Caddy."""

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
				"public_ipv4": PUBLIC_IPV4,
				"status": "Running",
			}
		).insert()
		self.server.db_set("machine", self.machine.name)
		self.server.reload()

	def steps(self, installed=True, routed=True, serving=True):
		return (
			patch.multiple(
				DatumServer,
				start_setup_on_machine=Mock(return_value=installed),
				get_datum_commit=Mock(return_value="c" * 40),
			),
			patch.object(DatumServer, "install_caddy", return_value=routed),
			patch.object(DatumServer, "check_if_proxy_is_configured", return_value=serving),
		)

	def test_an_install_that_failed_is_never_routed_to(self):
		install, caddy, probe = self.steps(installed=False)
		with install, caddy as installed_caddy, probe as probed:
			self.server._setup()

		installed_caddy.assert_not_called()
		probed.assert_not_called()

	def test_a_host_caddy_would_not_serve_is_never_probed(self):
		install, caddy, probe = self.steps(routed=False)
		with install, caddy, probe as probed:
			self.server._setup()

		probed.assert_not_called()
		self.server.reload()
		self.assertNotEqual(self.server.status, "Active")

	def test_setup_records_the_commit_it_installed(self):
		install, caddy, probe = self.steps()
		with install, caddy, probe:
			self.server._setup()

		self.server.reload()
		self.assertEqual(self.server.commit, "c" * 40)

	def test_setup_installs_the_pinned_commit(self):
		self.server.db_set("commit", "abc1234")
		self.server.reload()

		with patch("cargo.telemetry.doctype.datum_server.datum_server.run_over_ssh") as ran:
			self.server.start_setup_on_machine()

		self.assertIn("export DATUM_COMMIT=abc1234", ran.call_args_list[0].args[1])

	def test_a_blank_branch_falls_back_to_main(self):
		self.server.branch = ""
		self.server.save()

		self.assertEqual(self.server.branch, "main")

	def test_a_host_is_active_once_every_step_lands(self):
		install, caddy, probe = self.steps()
		with install, caddy, probe:
			self.server._setup()

		self.server.reload()
		self.assertEqual(self.server.status, "Active")

	def test_a_host_that_does_not_answer_publicly_is_not_active(self):
		install, caddy, probe = self.steps(serving=False)
		with install, caddy, probe:
			self.server._setup()

		self.server.reload()
		self.assertNotEqual(self.server.status, "Active")

	def test_a_host_caddy_refuses_says_so(self):
		with patch.object(Proxy, "install", side_effect=RuntimeError("caddy validate failed")):
			self.assertFalse(self.server.install_caddy())

		self.server.reload()
		self.assertEqual(self.server.status, "Failed")
		self.assertIn("Caddy", self.server.error)

	def test_a_host_that_never_answers_names_the_dns_to_point(self):
		with patch.object(Proxy, "wait_until_serving", return_value=False):
			self.assertFalse(self.server.check_if_proxy_is_configured())

		self.server.reload()
		self.assertEqual(self.server.status, "Failed")
		self.assertIn(f"telemetry-read-svc.{SETTINGS['wildcard_domain']}", self.server.error)
		self.assertIn(PUBLIC_IPV4, self.server.error)

	def test_setup_is_refused_before_the_host_has_a_public_address(self):
		frappe.db.set_value("Machine", self.machine.name, "public_ipv4", None)

		with (
			patch.object(DatumServer, "_setup") as flow,
			self.assertRaisesRegex(frappe.ValidationError, "public IPv4"),
		):
			self.server.setup()

		flow.run_as_workflow.assert_not_called()
		self.assertNotEqual(frappe.db.get_single_value("Datum Server", "status"), "Setting Up")

	def test_the_host_asks_atlas_for_a_public_address(self):
		self.server.db_set("machine", None)
		with (
			patch("cargo.telemetry.doctype.datum_server.datum_server.base_image_id"),
			patch("cargo.cargo.doctype.machine.machine.Machine.request") as request,
		):
			request.return_value.name = self.machine.name
			self.server.create_telemetry_node(cpu_millicores=1000, ram_gb=2, disk_gb=20)

		self.assertTrue(request.call_args.kwargs["has_public_ipv4"])


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


class IntegrationTestDatumMetricsExporter(IntegrationTestCase):
	"""Vector on the Datum host, pushing its own host metrics with the host's own token."""

	def setUp(self):
		IntegrationTestTelemetryRouting.setUp(self)
		module = "cargo.telemetry.doctype.datum_server.datum_server"
		self.atlas = self.enterContext(patch(f"{module}.AtlasClient.from_settings"))
		self.enterContext(patch(f"{module}.generate_machine_token", return_value="machine-token"))

	def ssh(self, **behaviour):
		return patch("cargo.telemetry.doctype.datum_server.datum_server.run_over_ssh", **behaviour)

	def test_the_host_labels_its_metrics_as_telemetry(self):
		with self.ssh() as ran:
			self.server.install_metrics_exporter()

		sent = ran.call_args_list[1].args[1]
		self.assertNotIn("export DATUM_TOKEN=", sent)
		self.assertIn(f"export ROLE={TELEMETRY}", sent)
		self.assertIn(f"export MACHINE={shlex.quote(self.machine.name)}", sent)
		self.assertIn(f"export REGION={SETTINGS['region']}", sent)
		self.assertNotIn("export CLUSTER=", sent)

	def test_the_host_keeps_its_role_and_gets_its_machine_token(self):
		with self.ssh() as ran:
			self.server.install_metrics_exporter()

		self.atlas.return_value.replace_vm_metadata.assert_called_once_with(
			self.machine.vm_id, {"role": TELEMETRY, "datum_telemetry_token": "machine-token"}
		)
		refresh = ran.call_args_list[0].args[1]
		self.assertIn("datum-refresh.timer", refresh)
		# Central only learns the endpoint once this host is Active, so Cargo names it.
		self.assertIn(f"export DATUM_ENDPOINT={shlex.quote(self.server.service_endpoint)}", refresh)

	def test_setup_installs_vector_once_datum_is_installed(self):
		with self.ssh() as ran:
			self.assertTrue(self.server.start_setup_on_machine())

		self.assertEqual(ran.call_count, 3)
		self.assertIn("datum-refresh.timer", ran.call_args_list[1].args[1])
		self.assertIn("export VECTOR_VERSION=", ran.call_args_list[2].args[1])

	def test_a_metrics_exporter_that_will_not_install_fails_the_host(self):
		"""Nothing hands the host a token later, so setup is the only chance."""
		with self.ssh(side_effect=["", RuntimeError("Central refused the machine token")]):
			self.assertFalse(self.server.start_setup_on_machine())

		self.server.reload()
		self.assertEqual(self.server.status, "Failed")
