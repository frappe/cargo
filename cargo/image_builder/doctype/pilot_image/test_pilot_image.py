# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

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

	def test_a_build_flushes_its_machine_while_it_still_holds_the_key(self):
		"""The snapshot task has no key, so a later flush could not reach the machine."""
		image = self.image(self.release())
		image.ssh_private_key = "key"
		image.save()

		with patch("cargo.image_builder.doctype.pilot_image.pilot_image.Builder") as builder:
			with patch("cargo.image_builder.doctype.pilot_image.pilot_image.OutputLog"):
				image.run_provision_script("fdaa:1::1d")

		builder.return_value.flush_build_machine.assert_called_once()
		self.assertEqual(image.status, "Snapshotting")
		self.assertIsNone(image.ssh_private_key)

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

		with patch("cargo.image_builder.doctype.pilot_image.builder.AtlasClient") as atlas:
			with self.assertRaises(frappe.ValidationError):
				self.image(self.release()).build()

		atlas.from_settings.assert_not_called()

	def test_a_build_without_a_region_stops_before_it_rents_a_machine(self):
		frappe.db.set_single_value("Cargo Settings", "region_id", 0)
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

		with patch("cargo.image_builder.doctype.pilot_image.builder.AtlasClient") as atlas:
			with self.assertRaises(frappe.ValidationError):
				self.image(self.release()).build()

		atlas.from_settings.assert_not_called()

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

	def building_image(self, status: str = "Building") -> PilotImage:
		"""An image part way through a build, with a machine rented for it."""
		image = self.image(self.release())
		image.db_set({"status": status, "temporary_vm_id": "vm-stuck", "ssh_public_key": "ssh-ed25519 AAAA"})
		image.reload()

		return image

	def test_a_stuck_build_can_be_stopped_and_stops_renting(self):
		"""What this exists for: the machine is what costs money, so it goes first."""
		image = self.building_image()

		with patch.object(PilotImage, "builder") as builder:
			builder.destroy_build_machine.return_value = True
			image.stop_build()

		builder.destroy_build_machine.assert_called_once_with("vm-stuck")
		self.assertEqual(image.status, "Failed")
		self.assertIsNone(image.temporary_vm_id)
		self.assertIn("stopped by", image.error)

	def test_a_machine_atlas_will_not_destroy_keeps_its_keys(self):
		"""The keypair still opens that machine, so it is kept until the machine is gone."""
		image = self.building_image()

		with patch.object(PilotImage, "builder") as builder:
			builder.destroy_build_machine.return_value = False
			image.stop_build()

		self.assertEqual(image.status, "Failed")
		self.assertEqual(image.temporary_vm_id, "vm-stuck")
		self.assertEqual(image.ssh_public_key, "ssh-ed25519 AAAA")

	def test_every_building_status_can_be_stopped(self):
		for status in ("Provisioning", "Building", "Snapshotting"):
			with self.subTest(status=status), patch.object(PilotImage, "builder") as builder:
				builder.destroy_build_machine.return_value = True
				image = self.building_image(status)
				image.stop_build()

				self.assertEqual(image.status, "Failed")

	def test_an_image_that_is_not_building_cannot_be_stopped(self):
		"""Draft, Available and Failed rent nothing, so there is nothing to stop."""
		image = self.image(self.release())

		with self.assertRaises(frappe.ValidationError):
			image.stop_build()
