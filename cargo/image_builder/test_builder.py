# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.atlas_client import AtlasError
from cargo.image_builder.doctype.pilot_image.builder import Builder
from cargo.ssh import SshError


class IntegrationTestBuilder(IntegrationTestCase):
	"""How a build waits for a machine that Atlas calls running before it has booted."""

	def setUp(self):
		self.builder = Builder()

	def ping(self, *returncodes):
		return patch(
			"cargo.image_builder.doctype.pilot_image.builder.subprocess.run",
			side_effect=[Mock(returncode=code) for code in returncodes],
		)

	def test_a_machine_that_answers_at_once_is_not_waited_for(self):
		with self.ping(0), patch("cargo.image_builder.doctype.pilot_image.builder.time.sleep") as sleep:
			self.assertTrue(self.builder.is_answering_ping("fdaa:1::1d"))

		sleep.assert_not_called()

	def test_a_machine_still_booting_is_pinged_again(self):
		with self.ping(1, 1, 0), patch("cargo.image_builder.doctype.pilot_image.builder.time.sleep"):
			self.assertTrue(self.builder.is_answering_ping("fdaa:1::1d"))

	def test_a_machine_that_never_answers_gives_up(self):
		with patch(
			"cargo.image_builder.doctype.pilot_image.builder.subprocess.run", return_value=Mock(returncode=1)
		):
			with patch(
				"cargo.image_builder.doctype.pilot_image.builder.time.monotonic", side_effect=[0, 999, 999]
			):
				with patch("cargo.image_builder.doctype.pilot_image.builder.time.sleep"):
					self.assertFalse(self.builder.is_answering_ping("fdaa:1::1d"))

	def test_sshd_that_is_not_up_yet_is_tried_again(self):
		with patch(
			"cargo.image_builder.doctype.pilot_image.builder.run_over_ssh",
			side_effect=[SshError("refused"), "up 1 min"],
		) as run:
			with patch("cargo.image_builder.doctype.pilot_image.builder.time.sleep"):
				self.assertTrue(self.builder.is_accepting_ssh("fdaa:1::1d", "key"))

		self.assertEqual(run.call_count, 2)

	def test_a_local_failure_keeps_its_own_error(self):
		"""A missing ssh binary says more than a readiness timeout, so it is not retried."""
		with patch(
			"cargo.image_builder.doctype.pilot_image.builder.run_over_ssh",
			side_effect=FileNotFoundError("ssh"),
		):
			with self.assertRaises(FileNotFoundError):
				self.builder.is_accepting_ssh("fdaa:1::1d", "key")

	def test_a_machine_that_never_accepts_ssh_gives_up(self):
		with patch(
			"cargo.image_builder.doctype.pilot_image.builder.run_over_ssh", side_effect=SshError("timed out")
		):
			with patch(
				"cargo.image_builder.doctype.pilot_image.builder.time.monotonic", side_effect=[0, 999, 999]
			):
				with patch("cargo.image_builder.doctype.pilot_image.builder.time.sleep"):
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

	def test_a_baked_machine_is_flushed_before_it_is_photographed(self):
		"""Atlas snapshots a paused disk, so what is only in the page cache is lost."""
		with patch("cargo.image_builder.doctype.pilot_image.builder.run_over_ssh") as run:
			self.builder.flush_build_machine("fdaa:1::1d", "key")

		self.assertEqual(run.call_args.args[1], "sync")

	def test_stop_waits_for_atlas_after_flushing(self):
		machine = Mock(vm_id="vm-builder", address="fdaa:1::1d")
		with (
			patch("cargo.atlas_client.AtlasClient.from_settings") as atlas,
			patch.object(Builder, "flush_build_machine") as flush,
			patch("cargo.image_builder.doctype.pilot_image.builder.time.sleep") as sleep,
		):
			client = atlas.return_value
			client.get_vm.side_effect = [
				{"current_state": state} for state in ("running", "running", "stopped")
			]
			client.set_power_state.side_effect = lambda *_: self.assertTrue(flush.called)
			self.builder.stop_build_machine(machine)

		client.set_power_state.assert_called_once_with("vm-builder", "stop")
		self.assertEqual(client.get_vm.call_count, 3)
		sleep.assert_called_once()

	def test_stopped_builder_does_not_need_ssh_on_retry(self):
		with (
			patch("cargo.atlas_client.AtlasClient.from_settings") as atlas,
			patch.object(Builder, "flush_build_machine") as flush,
		):
			atlas.return_value.get_vm.return_value = {"current_state": "stopped"}
			self.builder.stop_build_machine(Mock(vm_id="vm-builder"))
		flush.assert_not_called()

	def test_failed_flush_does_not_request_stop(self):
		with (
			patch("cargo.atlas_client.AtlasClient.from_settings") as atlas,
			patch.object(Builder, "flush_build_machine", side_effect=SshError("flush failed")),
		):
			atlas.return_value.get_vm.return_value = {"current_state": "running"}
			with self.assertRaisesRegex(SshError, "flush failed"):
				self.builder.stop_build_machine(Mock())
		atlas.return_value.set_power_state.assert_not_called()

	def test_stop_timeout_fails_capture_preparation(self):
		with (
			patch("cargo.atlas_client.AtlasClient.from_settings") as atlas,
			patch.object(Builder, "flush_build_machine"),
			patch("cargo.image_builder.doctype.pilot_image.builder.time.monotonic", side_effect=[0, 301]),
		):
			atlas.return_value.get_vm.return_value = {"current_state": "running"}
			with self.assertRaisesRegex(AtlasError, "did not stop within"):
				self.builder.stop_build_machine(Mock())

	def test_failed_or_unknown_machine_state_is_not_captured(self):
		for state in ("failed", "paused", None):
			with self.subTest(state=state), patch("cargo.atlas_client.AtlasClient.from_settings") as atlas:
				atlas.return_value.get_vm.side_effect = [
					{"current_state": "running"},
					{"current_state": state},
				]
				with (
					patch.object(Builder, "flush_build_machine"),
					self.assertRaisesRegex(AtlasError, "did not stop"),
				):
					self.builder.stop_build_machine(Mock())
