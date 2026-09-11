# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

from frappe.tests import UnitTestCase

from cargo.install import complete_setup_wizard


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
