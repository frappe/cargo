# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.client_models import TELEMETRY
from cargo.telemetry.doctype.datum_server.datum_server import DatumServer
from cargo.telemetry.spawn import (
	CONFIG_KEY,
	DATUM_FIELDS,
	MAX_SETUP_ATTEMPTS,
	ensure_telemetry,
	validate_config,
)
from cargo.testing import SETTINGS, use_test_settings

PEM = "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQ\n-----END PUBLIC KEY-----"

CONFIG = {
	"repository": "https://github.com/frappe/datum",
	"version": "develop",
	"clickhouse_host": "clickhouse.internal",
	TELEMETRY: {"cpu": 2, "ram_gb": 4, "disk_gb": 100},
}


class IntegrationTestTelemetryConfig(IntegrationTestCase):
	"""What a region must be told before Cargo builds it a datum host."""

	def refused(self, config: dict) -> bool:
		try:
			validate_config(config)
			return False
		except frappe.ValidationError:
			return True

	def test_the_config_this_region_would_build_from_is_accepted(self):
		self.assertFalse(self.refused(CONFIG))

	def test_anything_that_is_not_an_object_is_refused(self):
		for config in ("a string", 3, [CONFIG]):
			with self.subTest(config=config):
				self.assertTrue(self.refused(config))

	def test_a_datum_field_with_no_default_must_be_given(self):
		"""The host cannot be inserted without these, and they have no default to fall back
		on -- unlike base_image and clickhouse_port."""
		for field in DATUM_FIELDS:
			for value in (None, "", "   "):
				with self.subTest(field=field, value=value):
					self.assertTrue(self.refused({**CONFIG, field: value}))

	def test_the_machine_to_run_it_on_must_be_described(self):
		self.assertTrue(self.refused({key: value for key, value in CONFIG.items() if key != TELEMETRY}))

	def test_a_machine_size_that_cannot_be_rented_is_refused(self):
		for field in ("cpu", "ram_gb", "disk_gb"):
			for value in (0, -1, "2", None):
				with self.subTest(field=field, value=value):
					self.assertTrue(self.refused({**CONFIG, TELEMETRY: {**CONFIG[TELEMETRY], field: value}}))

	def test_a_host_told_nothing_builds_nothing(self):
		with patch.dict(frappe.local.conf, {CONFIG_KEY: None}), patch.object(frappe, "get_doc") as made:
			ensure_telemetry()

		made.assert_not_called()

	def test_an_unusable_config_is_logged_and_builds_nothing(self):
		with (
			patch.dict(frappe.local.conf, {CONFIG_KEY: {"repository": "only this"}}),
			patch.object(frappe, "log_error") as logged,
		):
			ensure_telemetry()

		self.assertIn(CONFIG_KEY, logged.call_args.kwargs["title"])


class SpawnTestCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		for doctype in ("Machine", "Datum Server"):
			frappe.db.delete(doctype)

	def configured(self, config: dict | None = CONFIG):
		return patch.dict(frappe.local.conf, {CONFIG_KEY: config})

	def atlas(self):
		client = patch("cargo.atlas_client.AtlasClient.from_settings").start()
		self.addCleanup(patch.stopall)
		client.return_value.create_vm.side_effect = lambda **kwargs: {
			"id": f"vm-{frappe.generate_hash(length=8)}"
		}

		return client.return_value

	def servers(self) -> list[str]:
		return frappe.get_all("Datum Server", pluck="name")

	def auto_server(self):
		return frappe.get_doc("Datum Server", {"auto_spawn": 1})

	def machine_of(self, server, status: str = "Running") -> str:
		machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": server.doctype,
				"reference_name": server.name,
				"role": TELEMETRY,
				"disk_size_gb": CONFIG[TELEMETRY]["disk_gb"],
				"vm_id": f"vm-{frappe.generate_hash(length=8)}",
				"address": "fdaa:1::7",
				"status": status,
			}
		).insert()
		server.db_set("machine", machine.name)
		server.reload()

		return machine.name


class IntegrationTestTelemetrySpawnCreation(SpawnTestCase):
	"""Which hosts build a datum server, and how many they build."""

	def test_a_fresh_host_builds_one_server(self):
		with self.configured():
			ensure_telemetry()

		server = self.auto_server()
		self.assertEqual(server.status, "Draft")
		self.assertEqual(server.repository, CONFIG["repository"])
		self.assertEqual(server.clickhouse_host, CONFIG["clickhouse_host"])

	def test_the_server_is_built_once_and_not_again(self):
		with self.configured():
			ensure_telemetry()
			ensure_telemetry()

		self.assertEqual(len(self.servers()), 1)

	def test_datum_checks_tokens_against_this_cargo_central(self):
		"""Neither an issuer nor a key means datum answers 401 to everything."""
		with self.configured():
			ensure_telemetry()

		self.assertEqual(self.auto_server().oidc_issuer, SETTINGS["central_url"])

	def test_a_server_added_by_hand_is_never_joined_by_a_second(self):
		frappe.get_doc(
			{
				"doctype": "Datum Server",
				"clickhouse_host": "clickhouse.internal",
				"repository": CONFIG["repository"],
				"version": CONFIG["version"],
				"public_key": PEM,
			}
		).insert()

		with self.configured():
			ensure_telemetry()

		self.assertEqual(len(self.servers()), 1)


class IntegrationTestTelemetrySpawnMachine(SpawnTestCase):
	"""The machine the host runs on."""

	def setUp(self):
		super().setUp()
		with self.configured():
			ensure_telemetry()
		self.server = self.auto_server()

	def test_a_machine_is_asked_for_at_the_size_the_config_says(self):
		atlas = self.atlas()
		with self.configured():
			ensure_telemetry()

		asked = atlas.create_vm.call_args.kwargs
		self.assertEqual(asked["vcpus"], CONFIG[TELEMETRY]["cpu"])
		self.assertTrue(self.auto_server().machine)

	def test_the_next_run_asks_for_nothing_more(self):
		atlas = self.atlas()
		self.machine_of(self.server, status="Pending")

		with self.configured():
			ensure_telemetry()

		atlas.create_vm.assert_not_called()

	def test_a_machine_that_would_not_boot_stops_the_run(self):
		self.machine_of(self.server, status="Broken")

		with self.configured(), patch.object(DatumServer, "setup") as setup:
			ensure_telemetry()

		setup.assert_not_called()
		self.assertIn("did not come up", self.auto_server().error)


class IntegrationTestTelemetrySpawnSetup(SpawnTestCase):
	"""Setting the host up once its machine is up."""

	def setUp(self):
		super().setUp()
		with self.configured():
			ensure_telemetry()
		self.server = self.auto_server()

	def run_once(self):
		with self.configured(), patch.object(DatumServer, "setup") as setup:
			ensure_telemetry()

		return setup

	def test_a_booted_machine_is_set_up(self):
		self.machine_of(self.server)

		self.run_once().assert_called_once()

	def test_a_machine_still_booting_waits(self):
		self.machine_of(self.server, status="Pending")

		self.run_once().assert_not_called()

	def test_a_run_under_way_is_left_alone(self):
		self.machine_of(self.server)
		self.server.db_set("status", "Setting Up")

		self.run_once().assert_not_called()

	def test_a_host_that_serves_is_left_alone(self):
		self.machine_of(self.server)
		self.server.db_set("status", "Active")

		self.run_once().assert_not_called()

	def test_a_failed_run_is_tried_again(self):
		self.machine_of(self.server)
		self.server.db_set("status", "Failed")

		self.run_once().assert_called_once()
		self.assertEqual(self.auto_server().auto_setup_attempts, 1)

	def test_trying_again_stops_once_the_budget_is_spent(self):
		self.machine_of(self.server)
		self.server.db_set({"status": "Failed", "auto_setup_attempts": MAX_SETUP_ATTEMPTS})

		self.run_once().assert_not_called()
