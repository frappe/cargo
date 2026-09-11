# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

from frappe.tests import UnitTestCase

from cargo.install import ENROLMENT_VARS, complete_setup_wizard, record_upstreams


class UnitTestCompleteSetupWizard(UnitTestCase):
	"""A provisioned host must land on the desk, not on the wizard."""

	def test_an_unfinished_wizard_is_walked_for_the_host(self):
		with (
			patch("frappe.is_setup_complete", return_value=False),
			patch("frappe.desk.page.setup_wizard.setup_wizard.setup_complete") as setup,
		):
			complete_setup_wizard()

		setup.assert_called_once_with({})

	def test_a_site_that_is_already_set_up_is_left_alone(self):
		with (
			patch("frappe.is_setup_complete", return_value=True),
			patch("frappe.desk.page.setup_wizard.setup_wizard.setup_complete") as setup,
		):
			complete_setup_wizard()

		setup.assert_not_called()


class UnitTestInstallSettings(UnitTestCase):
	def test_enrolment_uses_bearer_tokens_and_proxy_configuration(self) -> None:
		self.assertIn("ATLAS_TOKEN", ENROLMENT_VARS)
		self.assertIn("PROXY_TOKEN", ENROLMENT_VARS)
		self.assertIn("WILDCARD_DOMAIN", ENROLMENT_VARS)
		self.assertNotIn("ATLAS_KEY", ENROLMENT_VARS)
		self.assertNotIn("ATLAS_SECRET", ENROLMENT_VARS)

	def test_install_records_the_complete_environment_contract(self) -> None:
		settings = MagicMock()
		environment = {
			"CENTRAL_URL": "https://central.invalid",
			"ATLAS_URL": "https://atlas.example.com",
			"CARGO_URL": "https://cargo.example.com",
			"REGION_ID": "3",
			"REGION": "blr",
			"ATLAS_TOKEN": "atlas-token",
			"ATLAS_TENANT_ID": "0",
			"PROXY_URL": "https://proxy.example.com",
			"PROXY_TOKEN": "proxy-token",
			"WILDCARD_DOMAIN": "example.com",
			"CENTRAL_WEBHOOK_SECRET": "not-configured",
			"JWKS_URL": "https://atlas.example.com/api/atlas/jwks.json",
		}
		with (
			patch("cargo.install.frappe.get_single", return_value=settings),
			patch.dict("cargo.install.os.environ", environment, clear=True),
		):
			record_upstreams()

		self.assertEqual(settings.atlas_token, "atlas-token")
		self.assertEqual(settings.atlas_tenant_id, "0")
		self.assertEqual(settings.proxy_url, "https://proxy.example.com")
		self.assertEqual(settings.proxy_token, "proxy-token")
		self.assertEqual(settings.wildcard_domain, "example.com")
		settings.save.assert_called_once_with(ignore_permissions=True)
