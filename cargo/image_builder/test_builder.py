# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.image_builder.builder import Builder
from cargo.ssh import SshError


class IntegrationTestBuilder(IntegrationTestCase):
	"""How a build waits for a machine that Atlas calls running before it has booted."""

	def setUp(self):
		self.builder = Builder("IMG-0001-version-16")

	def ping(self, *returncodes):
		return patch(
			"cargo.image_builder.builder.subprocess.run",
			side_effect=[Mock(returncode=code) for code in returncodes],
		)

	def test_a_machine_that_answers_at_once_is_not_waited_for(self):
		with self.ping(0), patch("cargo.image_builder.builder.time.sleep") as sleep:
			self.assertTrue(self.builder.is_answering_ping("fdaa:1::1d"))

		sleep.assert_not_called()

	def test_a_machine_still_booting_is_pinged_again(self):
		with self.ping(1, 1, 0), patch("cargo.image_builder.builder.time.sleep"):
			self.assertTrue(self.builder.is_answering_ping("fdaa:1::1d"))

	def test_a_machine_that_never_answers_gives_up(self):
		with patch("cargo.image_builder.builder.subprocess.run", return_value=Mock(returncode=1)):
			with patch("cargo.image_builder.builder.time.monotonic", side_effect=[0, 999, 999]):
				with patch("cargo.image_builder.builder.time.sleep"):
					self.assertFalse(self.builder.is_answering_ping("fdaa:1::1d"))

	def test_sshd_that_is_not_up_yet_is_tried_again(self):
		with patch(
			"cargo.image_builder.builder.run_over_ssh", side_effect=[SshError("refused"), "up 1 min"]
		) as run:
			with patch("cargo.image_builder.builder.time.sleep"):
				self.assertTrue(self.builder.is_accepting_ssh("fdaa:1::1d", "key"))

		self.assertEqual(run.call_count, 2)

	def test_a_machine_that_never_accepts_ssh_gives_up(self):
		with patch("cargo.image_builder.builder.run_over_ssh", side_effect=SshError("timed out")):
			with patch("cargo.image_builder.builder.time.monotonic", side_effect=[0, 999, 999]):
				with patch("cargo.image_builder.builder.time.sleep"):
					self.assertFalse(self.builder.is_accepting_ssh("fdaa:1::1d", "key"))

	def test_a_machine_that_never_reaches_the_mesh_stops_the_build(self):
		with patch.object(Builder, "is_answering_ping", return_value=False):
			with self.assertRaises(frappe.ValidationError):
				self.builder.wait_until_reachable("fdaa:1::1d", "key")

	def test_a_machine_that_pings_but_refuses_ssh_stops_the_build(self):
		with patch.object(Builder, "is_answering_ping", return_value=True):
			with patch.object(Builder, "is_accepting_ssh", return_value=False):
				with self.assertRaises(frappe.ValidationError):
					self.builder.wait_until_reachable("fdaa:1::1d", "key")
