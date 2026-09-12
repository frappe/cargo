# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.image_builder.doctype.image.image import (
	ADMIN_DOMAIN,
	BENCH_NAME,
	SITE_NAME,
	Image,
	build_image,
	ensure_release,
	retire_old_releases,
	sync_pilot_releases,
	tracked_pilot_versions,
)
from cargo.testing import SETTINGS, use_test_settings

WILDCARD_DOMAIN = SETTINGS["wildcard_domain"]


class IntegrationTestImage(IntegrationTestCase):
	"""What an image hands its provision script, and how releases come and go."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()

	def image(self, pilot_version: str, frappe_version: str = "version-16") -> Image:
		image: Image = frappe.get_doc(
			{
				"doctype": "Image",
				"pilot_version": pilot_version,
				"frappe_version": frappe_version,
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
		self.assertEqual(environment["ADMIN_DOMAIN"], ADMIN_DOMAIN)
		self.assertEqual(environment["WILDCARD_DOMAIN"], WILDCARD_DOMAIN)
		self.assertEqual(environment["SITE"], SITE_NAME)
		self.assertEqual(environment["BENCH"], BENCH_NAME)

	def test_cargo_keeps_no_password_from_a_build(self):
		"""The script makes its own, so nothing to store and nothing to leak."""
		environment = self.image(self.release()).provision_environment

		self.assertNotIn("ADMIN_PASSWORD", environment)

	def test_every_image_carries_the_same_bench_and_site(self):
		first = self.image(self.release()).provision_environment
		second = self.image(self.release(), frappe_version="develop").provision_environment

		self.assertEqual(first["BENCH"], second["BENCH"])
		self.assertEqual(first["SITE"], second["SITE"])

	def test_one_release_cannot_take_the_same_frappe_version_twice(self):
		version = self.release()
		self.image(version)

		with self.assertRaises(frappe.ValidationError):
			self.image(version)

	def test_a_build_without_a_wildcard_domain_stops_before_it_rents_a_machine(self):
		frappe.db.set_single_value("Cargo Settings", "wildcard_domain", "")
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

		with patch("cargo.image_builder.builder.AtlasClient") as atlas:
			with self.assertRaises(frappe.ValidationError):
				self.image(self.release()).build()

		atlas.from_settings.assert_not_called()

	def test_a_release_becomes_one_image_per_frappe_version(self):
		version = self.release()
		waiting = ensure_release(version)

		self.assertEqual(len(waiting), 2)
		self.assertEqual(
			sorted(frappe.get_all("Image", {"pilot_version": version}, pluck="frappe_version")),
			["develop", "version-16"],
		)

	def test_a_release_whose_build_started_is_left_alone(self):
		version = self.release()
		ensure_release(version)
		frappe.db.set_value("Image", {"pilot_version": version}, "status", "Provisioning")

		self.assertEqual(ensure_release(version), [])

	def test_an_image_whose_build_never_started_is_tried_again(self):
		version = self.release()
		self.image(version).db_set("status", "Draft")

		# The draft left behind, and the Frappe version that had no image yet.
		self.assertEqual(len(ensure_release(version)), 2)

	def test_each_image_is_built_in_a_job_of_its_own(self):
		version = self.release()
		frappe.db.set_single_value("Cargo Settings", "track_pilot_releases", 1)
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

		with patch("cargo.image_builder.doctype.image.image.latest_pilot_release", return_value=version):
			with patch("cargo.image_builder.doctype.image.image.frappe.enqueue") as enqueue:
				sync_pilot_releases()

		# Frappe enqueues its own work on insert, so only this app's jobs are counted.
		calls = [call for call in enqueue.call_args_list if "build_image" in call.args[0]]
		self.assertEqual(len(calls), 2)
		self.assertTrue(all(call.kwargs["deduplicate"] for call in calls))

	def test_a_queued_build_that_already_started_does_nothing(self):
		image = self.image(self.release())
		image.db_set("status", "Provisioning")

		with patch.object(Image, "build") as build:
			build_image(image.name)

		build.assert_not_called()

	def test_tracking_is_off_until_the_setting_turns_it_on(self):
		with patch("cargo.image_builder.doctype.image.image.latest_pilot_release") as latest:
			sync_pilot_releases()

		latest.assert_not_called()

	def test_tracking_builds_the_newest_release_when_it_is_on(self):
		frappe.db.set_single_value("Cargo Settings", "track_pilot_releases", 1)
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")
		version = self.release()

		with patch("cargo.image_builder.doctype.image.image.latest_pilot_release", return_value=version):
			with patch("cargo.image_builder.doctype.image.image.frappe.enqueue"):
				sync_pilot_releases()

		self.assertEqual(frappe.db.count("Image", {"pilot_version": version}), 2)

	def retire_names(self) -> tuple[list[str], object]:
		"""Collect what `retire_old_releases` would drop, without dropping it."""
		retired: list[str] = []
		patched = patch.object(
			Image, "retire", autospec=True, side_effect=lambda image: retired.append(image.name)
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
		frappe.db.set_value("Image", names[versions[0]], "status", "Building")

		retired, patched = self.retire_names()
		with patched:
			retire_old_releases()

		self.assertNotIn(names[versions[0]], retired)

	def test_retiring_drops_the_snapshot_before_the_record(self):
		image = self.image(self.release())
		image.db_set("snapshot_id", "img-1")

		with patch("cargo.image_builder.doctype.image.image.AtlasClient") as atlas:
			self.assertTrue(image.retire())

		atlas.from_settings.return_value.delete_snapshot.assert_called_once_with("img-1")
		self.assertFalse(frappe.db.exists("Image", image.name))

	def test_a_snapshot_atlas_refuses_to_drop_keeps_its_record(self):
		image = self.image(self.release())
		image.db_set("snapshot_id", "img-2")

		with patch("cargo.image_builder.doctype.image.image.AtlasClient") as atlas:
			atlas.from_settings.return_value.delete_snapshot.side_effect = Exception("busy")
			self.assertFalse(image.retire())

		self.assertTrue(frappe.db.exists("Image", image.name))
