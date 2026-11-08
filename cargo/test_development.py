from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from cargo.development import is_mesh_only_enabled
from cargo.telemetry.spawn import ensure_telemetry


class UnitTestDevelopment(UnitTestCase):
	def test_mesh_requires_developer_mode(self):
		with patch.dict(frappe.conf, {"dev_mesh_only_proxy": True, "developer_mode": False}):
			with self.assertRaises(frappe.ValidationError):
				is_mesh_only_enabled()

	def test_disabled_telemetry_never_enters_spawn(self):
		with (
			patch.dict(
				frappe.conf,
				{"dev_mesh_only_proxy": True, "developer_mode": True, "dev_cargo_disable_telemetry": True},
			),
			patch.object(frappe.db, "get_single_value", return_value="metal.test"),
			patch("cargo.telemetry.spawn.spawn_config") as config,
		):
			ensure_telemetry()
			config.assert_not_called()

	def test_disabled_metrics_does_not_fetch_a_datum_token(self):
		from cargo.object_storage.doctype.object_storage_cluster.setup import Setup

		with (
			patch(
				"cargo.object_storage.doctype.object_storage_cluster.setup.is_telemetry_disabled",
				return_value=True,
			),
			patch.object(Setup, "run") as run,
		):
			Setup.install_metrics_exporter(None, {})
			run.assert_not_called()
