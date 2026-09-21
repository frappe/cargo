# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.cargo.doctype.machine.machine import Machine as MachineDoc
from cargo.image_builder.doctype.pilot_image.pilot_image import (
	PilotImage,
	build_image,
	ensure_release,
	retire_old_releases,
	sync_pilot_releases,
	tracked_pilot_versions,
)
from cargo.testing import SETTINGS, use_test_settings

WILDCARD_DOMAIN = SETTINGS["wildcard_domain"]


class IntegrationTestPilotImage(IntegrationTestCase):
	"""What an image hands its provision script, and how releases come and go."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()

	def image(self, pilot_version: str, frappe_version: str = "version-16", has_site: int = 1) -> PilotImage:
		image: PilotImage = frappe.get_doc(
			{
				"doctype": "Pilot Image",
				"pilot_version": pilot_version,
				"frappe_version": frappe_version,
				"has_site": has_site,
			}
		).insert()

		return image

	def machine(self, image: PilotImage, status: str = "Running", address: str = "fdaa:1::1d") -> str:
		"""A build machine Atlas already answered for, linked to the image."""
		machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": image.doctype,
				"reference_name": image.name,
				"role": "builder",
				"disk_size_gb": 8,
				"vm_id": f"vm-{frappe.generate_hash(length=8)}",
				"address": address,
				"status": status,
			}
		).insert()
		image.db_set("machine", machine.name)
		image.reload()

		return machine.name

	def release(self) -> str:
		"""A release nothing else in this test run uses."""
		return f"v0.0.32-{frappe.generate_hash(length=6)}"

	def test_an_image_names_every_value_its_script_reads(self):
		version = self.release()
		environment = self.image(version).provision_environment

		self.assertEqual(environment["VERSION"], version)
		self.assertEqual(environment["FRAPPE_VERSION"], "version-16")
		self.assertEqual(environment["WILDCARD_DOMAIN"], WILDCARD_DOMAIN)
		self.assertEqual(environment["PROXY_SUBNET"], f"fdaa:{SETTINGS['region_id']:x}::/64")

	def test_an_image_bakes_its_region_into_the_domain_provider(self):
		provider = self.image(self.release()).domain_provider

		self.assertIn(f'["*.{WILDCARD_DOMAIN}"]', provider)
		self.assertIn(f'["fdaa:{SETTINGS["region_id"]:x}::/64"]', provider)
		self.assertNotIn("__WILDCARD_DOMAIN__", provider)
		self.assertNotIn("__PROXY_SUBNET__", provider)

	def test_an_image_tells_the_script_whether_to_make_a_site(self):
		with_site = self.image(self.release())
		without_site = self.image(self.release(), has_site=0)

		self.assertEqual(with_site.provision_environment["HAS_SITE"], "1")
		self.assertEqual(without_site.provision_environment["HAS_SITE"], "0")

	def test_a_snapshot_is_labelled_with_whether_it_carries_a_site(self):
		"""Central picks an image by its tags, so the two variants have to differ there."""
		self.assertEqual(self.image(self.release()).image_tags["has_site"], "1")
		self.assertEqual(self.image(self.release(), has_site=0).image_tags["has_site"], "0")

	def test_cargo_keeps_no_password_from_a_build(self):
		"""The script makes its own, so nothing to store and nothing to leak."""
		environment = self.image(self.release()).provision_environment

		self.assertNotIn("ADMIN_PASSWORD", environment)

	def test_the_script_names_the_bench_site_and_admin_domain(self):
		"""Every image carries the same three, so Cargo does not hand them out."""
		environment = self.image(self.release()).provision_environment

		self.assertNotIn("BENCH", environment)
		self.assertNotIn("SITE", environment)
		self.assertNotIn("ADMIN_DOMAIN", environment)

	def test_a_build_flushes_its_machine_before_it_is_snapshotted(self):
		"""Atlas photographs a paused disk, so the flush belongs to the provision task."""
		image = self.image(self.release())
		self.machine(image)

		with patch("cargo.image_builder.doctype.pilot_image.pilot_image.Builder") as builder:
			with patch("cargo.image_builder.doctype.pilot_image.pilot_image.OutputLog"):
				image.run_provision_script("fdaa:1::1d")

		builder.return_value.flush_build_machine.assert_called_once()
		self.assertEqual(image.status, "Snapshotting")

	def test_a_build_reads_the_key_off_its_machine(self):
		"""The keypair belongs to the machine it opens, so the image keeps no copy."""
		image = self.image(self.release())
		machine = self.machine(image)

		with patch("cargo.image_builder.doctype.pilot_image.pilot_image.Builder") as builder:
			with patch("cargo.image_builder.doctype.pilot_image.pilot_image.OutputLog"):
				image.run_provision_script("fdaa:1::1d")

		private_key = frappe.get_doc("Machine", machine).get_password("ssh_private_key")
		builder.return_value.wait_until_reachable.assert_called_once_with("fdaa:1::1d", private_key)

	def test_a_build_asks_for_one_builder_machine(self):
		image = self.image(self.release())
		machine = frappe.get_doc("Machine", self.machine(image))

		with (
			patch("cargo.image_builder.doctype.pilot_image.pilot_image.base_image_id", return_value="img-1"),
			patch.object(MachineDoc, "request", return_value=machine) as request,
		):
			image.build()

		spec = request.call_args.args[1]
		self.assertEqual(spec.role, "builder")
		self.assertEqual(request.call_args.kwargs["base_image"], "img-1")
		self.assertEqual(image.machine, machine.name)
		self.assertEqual(image.status, "Provisioning")

	def test_the_snapshot_is_taken_off_the_machine_and_the_machine_let_go(self):
		image = self.image(self.release())
		machine = self.machine(image)

		with (
			patch.object(MachineDoc, "snapshot", return_value="img-9") as snapshot,
			patch.object(MachineDoc, "terminate", return_value=True) as terminate,
		):
			image.take_snapshot()

		snapshot.assert_called_once_with(image.atlas_name, image.image_tags)
		terminate.assert_called_once()
		self.assertEqual(image.snapshot_id, "img-9")
		self.assertEqual(frappe.db.get_value("Pilot Image", image.name, "machine"), machine)

	def test_a_machine_that_came_up_starts_the_build(self):
		image = self.image(self.release())
		self.machine(image)
		image.db_set("status", "Provisioning")
		image.reload()

		with patch.object(PilotImage, "run_build") as run_build:
			image.sync_machines()

		run_build.run_as_workflow.assert_called_once_with(address="fdaa:1::1d")
		self.assertEqual(image.status, "Building")

	def test_a_machine_that_never_came_up_fails_the_image(self):
		image = self.image(self.release())
		machine = self.machine(image, status="Broken")
		frappe.db.set_value("Machine", machine, "error", "Atlas reported failed")
		image.db_set("status", "Provisioning")
		image.reload()

		with patch.object(PilotImage, "run_build") as run_build:
			image.sync_machines()

		run_build.run_as_workflow.assert_not_called()
		self.assertEqual(image.status, "Failed")
		self.assertIn("Atlas reported failed", image.error)

	def test_a_machine_still_booting_leaves_the_image_waiting(self):
		image = self.image(self.release())
		self.machine(image, status="Pending", address=None)
		image.db_set("status", "Provisioning")
		image.reload()

		with patch.object(PilotImage, "run_build") as run_build:
			image.sync_machines()

		run_build.run_as_workflow.assert_not_called()
		self.assertEqual(image.status, "Provisioning")

	def test_one_release_cannot_take_the_same_frappe_version_twice(self):
		version = self.release()
		self.image(version)

		with self.assertRaises(frappe.ValidationError):
			self.image(version)

	def test_the_same_release_can_be_baked_with_and_without_a_site(self):
		version = self.release()
		self.image(version)

		self.assertTrue(self.image(version, has_site=0).name)

	def test_a_build_without_a_wildcard_domain_stops_before_it_rents_a_machine(self):
		frappe.db.set_single_value("Cargo Settings", "wildcard_domain", "")
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

		with patch("cargo.image_builder.doctype.pilot_image.pilot_image.MachineDoc") as machine:
			with self.assertRaises(frappe.ValidationError):
				self.image(self.release()).build()

		machine.request.assert_not_called()

	def test_a_build_without_a_region_stops_before_it_rents_a_machine(self):
		frappe.db.set_single_value("Cargo Settings", "region_id", 0)
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

		with patch("cargo.image_builder.doctype.pilot_image.pilot_image.MachineDoc") as machine:
			with self.assertRaises(frappe.ValidationError):
				self.image(self.release()).build()

		machine.request.assert_not_called()

	def test_a_release_becomes_an_image_per_frappe_version_and_site_variant(self):
		version = self.release()
		waiting = ensure_release(version)

		self.assertEqual(len(waiting), 4)
		self.assertEqual(
			sorted(frappe.get_all("Pilot Image", {"pilot_version": version}, pluck="frappe_version")),
			["develop", "develop", "version-16", "version-16"],
		)
		self.assertEqual(frappe.db.count("Pilot Image", {"pilot_version": version, "has_site": 0}), 2)

	def test_an_image_a_release_already_has_is_not_made_twice(self):
		"""One made by hand is that variant of the release, not a fifth image."""
		version = self.release()
		existing = self.image(version, has_site=0)

		waiting = ensure_release(version)

		self.assertIn(existing.name, waiting)
		self.assertEqual(frappe.db.count("Pilot Image", {"pilot_version": version}), 4)

	def test_a_release_whose_build_started_is_left_alone(self):
		version = self.release()
		ensure_release(version)
		frappe.db.set_value("Pilot Image", {"pilot_version": version}, "status", "Provisioning")

		self.assertEqual(ensure_release(version), [])

	def test_an_image_whose_build_never_started_is_tried_again(self):
		version = self.release()
		self.image(version).db_set("status", "Draft")

		# The draft left behind, and the three variants that had no image yet.
		self.assertEqual(len(ensure_release(version)), 4)

	def test_each_image_is_built_in_a_job_of_its_own(self):
		version = self.release()
		frappe.db.set_single_value("Cargo Settings", "track_pilot_releases", 1)
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

		with patch(
			"cargo.image_builder.doctype.pilot_image.pilot_image.latest_pilot_release",
			return_value=version,
		):
			with patch("cargo.image_builder.doctype.pilot_image.pilot_image.frappe.enqueue") as enqueue:
				sync_pilot_releases()

		# Frappe enqueues its own work on insert, so only this app's jobs are counted.
		calls = [call for call in enqueue.call_args_list if "build_image" in call.args[0]]
		self.assertEqual(len(calls), 4)
		self.assertTrue(all(call.kwargs["deduplicate"] for call in calls))

	def test_a_queued_build_that_already_started_does_nothing(self):
		image = self.image(self.release())
		image.db_set("status", "Provisioning")

		with patch.object(PilotImage, "build") as build:
			build_image(image.name)

		build.assert_not_called()

	def test_tracking_is_off_until_the_setting_turns_it_on(self):
		with patch("cargo.image_builder.doctype.pilot_image.pilot_image.latest_pilot_release") as latest:
			sync_pilot_releases()

		latest.assert_not_called()

	def test_tracking_builds_the_newest_release_when_it_is_on(self):
		frappe.db.set_single_value("Cargo Settings", "track_pilot_releases", 1)
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")
		version = self.release()

		with patch(
			"cargo.image_builder.doctype.pilot_image.pilot_image.latest_pilot_release",
			return_value=version,
		):
			with patch("cargo.image_builder.doctype.pilot_image.pilot_image.frappe.enqueue"):
				sync_pilot_releases()

		self.assertEqual(frappe.db.count("Pilot Image", {"pilot_version": version}), 4)

	def retire_names(self) -> tuple[list[str], object]:
		"""Collect what `retire_old_releases` would drop, without dropping it."""
		retired: list[str] = []
		patched = patch.object(
			PilotImage, "retire", autospec=True, side_effect=lambda image: retired.append(image.name)
		)

		return retired, patched

	def test_the_window_keeps_the_newest_releases_and_retires_the_rest(self):
		versions = [self.release() for _ in range(4)]
		names = {version: self.image(version).name for version in versions}

		self.assertEqual(tracked_pilot_versions(), list(reversed(versions[1:])))

		retired, patched = self.retire_names()
		with patched:
			retire_old_releases()

		self.assertIn(names[versions[0]], retired)
		for version in versions[1:]:
			self.assertNotIn(names[version], retired)

	def test_an_image_being_built_is_never_retired(self):
		versions = [self.release() for _ in range(4)]
		names = {version: self.image(version).name for version in versions}
		frappe.db.set_value("Pilot Image", names[versions[0]], "status", "Building")

		retired, patched = self.retire_names()
		with patched:
			retire_old_releases()

		self.assertNotIn(names[versions[0]], retired)

	def test_retiring_drops_the_snapshot_before_the_record(self):
		image = self.image(self.release())
		image.db_set("snapshot_id", "img-1")

		with patch("cargo.image_builder.doctype.pilot_image.pilot_image.AtlasClient") as atlas:
			self.assertTrue(image.retire())

		atlas.from_settings.return_value.delete_snapshot.assert_called_once_with("img-1")
		self.assertFalse(frappe.db.exists("Pilot Image", image.name))

	def test_a_snapshot_atlas_refuses_to_drop_keeps_its_record(self):
		image = self.image(self.release())
		image.db_set("snapshot_id", "img-2")

		with patch("cargo.image_builder.doctype.pilot_image.pilot_image.AtlasClient") as atlas:
			atlas.from_settings.return_value.delete_snapshot.side_effect = Exception("busy")
			self.assertFalse(image.retire())

		self.assertTrue(frappe.db.exists("Pilot Image", image.name))

	def workflow(self, image: PilotImage, status: str = "Success") -> str:
		"""A build workflow linked to the image, without running it."""
		workflow = frappe.get_doc(
			{
				"doctype": "Press Workflow",
				"linked_doctype": image.doctype,
				"linked_docname": image.name,
				"main_method_name": "build",
				"main_method_title": "Build",
				"status": status,
			}
		)
		with patch("cargo.workflow_engine.doctype.press_workflow.press_workflow.enqueue_workflow"):
			workflow.insert(ignore_permissions=True)

		return workflow.name

	def test_retiring_takes_the_workflows_that_link_to_the_image(self):
		image = self.image(self.release())
		workflow = self.workflow(image)

		with patch("cargo.image_builder.doctype.pilot_image.pilot_image.AtlasClient"):
			self.assertTrue(image.retire())

		self.assertFalse(frappe.db.exists("Press Workflow", workflow))
		self.assertFalse(frappe.db.exists("Pilot Image", image.name))

	def test_an_image_a_workflow_still_runs_on_is_kept(self):
		image = self.image(self.release())
		workflow = self.workflow(image, status="Running")

		with patch("cargo.image_builder.doctype.pilot_image.pilot_image.AtlasClient"):
			with self.assertRaises(frappe.ValidationError):
				image.retire()

		self.assertTrue(frappe.db.exists("Press Workflow", workflow))

	def test_an_image_that_will_not_retire_leaves_the_rest_of_the_window_alone(self):
		versions = [self.release() for _ in range(5)]
		names = {version: self.image(version).name for version in versions}
		stuck = names[versions[0]]
		retired: list[str] = []

		def retire(image: PilotImage) -> None:
			if image.name == stuck:
				raise frappe.LinkExistsError("still linked")
			retired.append(image.name)

		# A real rollback would drop the rows this test made.
		with (
			patch.object(PilotImage, "retire", autospec=True, side_effect=retire),
			patch.object(frappe.db, "rollback") as rollback,
		):
			retire_old_releases()

		self.assertIn(names[versions[1]], retired)
		rollback.assert_called_once()
		self.assertTrue(frappe.db.exists("Pilot Image", stuck))

	def building_image(self, status: str = "Building") -> PilotImage:
		"""An image part way through a build, with a machine rented for it."""
		image = self.image(self.release())
		self.machine(image)
		image.db_set("status", status)
		image.reload()

		return image

	def test_a_started_build_is_asked_to_fail_itself(self):
		"""Its own failure callback destroys the machine and records which step died, so
		nothing is torn down from here."""
		image = self.building_image()
		workflow = self.workflow(image, status="Running")

		with patch.object(MachineDoc, "terminate") as terminate:
			image.stop_build()

		terminate.assert_not_called()
		self.assertTrue(frappe.db.get_value("Press Workflow", workflow, "is_force_failure_requested"))

	def test_a_workflow_a_restarted_worker_left_running_is_stopped_the_same_way(self):
		"""Nothing is executing it, so only the flag `retry_workflows` reads can end it."""
		image = self.building_image()
		workflow = self.workflow(image, status="Queued")

		with patch.object(MachineDoc, "terminate"):
			image.stop_build()

		self.assertTrue(frappe.db.get_value("Press Workflow", workflow, "is_force_failure_requested"))

	def test_a_build_with_no_workflow_yet_releases_its_own_machine(self):
		"""Provisioning rents a machine before the workflow exists, so nothing else will."""
		image = self.building_image(status="Provisioning")

		with patch.object(MachineDoc, "terminate", return_value=True) as terminate:
			image.stop_build()

		terminate.assert_called_once()
		self.assertEqual(image.status, "Failed")
		self.assertIn("stopped by", image.error)

	def test_a_machine_atlas_will_not_destroy_leaves_the_build_alone(self):
		"""Failed reads as renting nothing, so it is not written over a machine still up."""
		image = self.building_image(status="Provisioning")

		with patch.object(MachineDoc, "terminate", return_value=False):
			with self.assertRaises(frappe.ValidationError):
				image.stop_build()

		self.assertEqual(image.status, "Provisioning")
		self.assertEqual(frappe.db.get_value("Machine", image.machine, "status"), "Running")

	def test_an_image_that_is_not_building_cannot_be_stopped(self):
		"""Draft, Available and Failed rent nothing, so there is nothing to stop."""
		image = self.image(self.release())

		with self.assertRaises(frappe.ValidationError):
			image.stop_build()
