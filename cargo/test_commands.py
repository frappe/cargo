# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import call, patch

from frappe.exceptions import SiteNotSpecifiedError
from frappe.tests import UnitTestCase

from cargo.commands import commands, set_pilot_release_tracking


class UnitTestCommands(UnitTestCase):
	def test_commands_toggle_pilot_release_tracking(self):
		self.assertEqual(
			[command.name for command in commands],
			["enable-pilot-release-tracker", "disable-pilot-release-tracker"],
		)

	def test_release_tracking_is_set_for_every_selected_site(self):
		context = SimpleNamespace(sites=["first.localhost", "second.localhost"])
		with patch("cargo.commands.frappe") as frappe:
			set_pilot_release_tracking(context, enabled=True)

		self.assertEqual(frappe.init.call_args_list, [call("first.localhost"), call("second.localhost")])
		self.assertEqual(frappe.connect.call_count, 2)
		self.assertEqual(
			frappe.db.set_single_value.call_args_list,
			[
				call("Cargo Settings", "track_pilot_releases", True),
				call("Cargo Settings", "track_pilot_releases", True),
			],
		)
		self.assertEqual(frappe.db.commit.call_count, 2)
		self.assertEqual(frappe.destroy.call_count, 2)

	def test_release_tracking_can_be_disabled(self):
		context = SimpleNamespace(sites=["cargo.localhost"])
		with patch("cargo.commands.frappe") as frappe:
			set_pilot_release_tracking(context, enabled=False)

		frappe.db.set_single_value.assert_called_once_with(
			"Cargo Settings", "track_pilot_releases", False
		)

	def test_release_tracking_requires_a_site(self):
		with self.assertRaises(SiteNotSpecifiedError):
			set_pilot_release_tracking(SimpleNamespace(sites=[]), enabled=True)
