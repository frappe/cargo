# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.atlas_client import AtlasNotFound
from cargo.image_builder.doctype.pilot_image.pilot_image import PilotImage
from cargo.image_builder.doctype.pilot_image_snapshot.pilot_image_snapshot import PilotImageSnapshot
from cargo.testing import use_test_settings

CONTROLLER = "cargo.image_builder.doctype.pilot_image_snapshot.pilot_image_snapshot"
MACHINE = SimpleNamespace(address="fdaa:1::1d", get_password=lambda fieldname: "private-key")


class IntegrationTestPilotImageSnapshot(IntegrationTestCase):
	"""What a snapshot does to the site around its photograph, and how it learns it is done."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()
		frappe.db.set_single_value("Cargo Settings", "version_supporting_app_toggle", ">=17.0.0-dev")
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

	def image(self, frappe_version: str = "16.35.0", image_type: str = "Apps") -> PilotImage:
		with patch.object(PilotImage, "after_insert"):
			image: PilotImage = frappe.get_doc(
				{
					"doctype": "Pilot Image",
					"pilot_version": f"v0.0.1-{frappe.generate_hash(length=6)}",
					"frappe_branch": "develop" if frappe_version.startswith("17") else "version-16",
					"frappe_version": frappe_version,
					"image_type": image_type,
				}
			).insert()

		return image

	def snapshot(self, image: PilotImage, signup_app: str | None, apps: list[str]) -> PilotImageSnapshot:
		return frappe.get_doc(
			{
				"doctype": "Pilot Image Snapshot",
				"pilot_image": image.name,
				"status": "Snapshotting",
				"signup_app": signup_app,
				"snapshot_id": "cargo-snapshot/img-1",
				"required_apps": [
					{
						"app": app,
						"version": "1.0.0",
						"repo": f"https://github.com/frappe/{app}",
						"commit": f"{app}-commit",
					}
					for app in apps
				],
			}
		).insert()

	def site_changes(self, run, image: PilotImage, snapshot: PilotImageSnapshot) -> list[tuple]:
		"""The `snapshot_apps.sh` runs a step asks for, as (action, apps)."""
		with patch(f"{CONTROLLER}.Builder") as builder:
			run(snapshot, MACHINE, image)

		return [call.args[2:] for call in builder.return_value.change_site_apps.call_args_list]

	def test_version_16_installs_the_apps_and_checks_nothing_else_is_there(self):
		image = self.image()
		snapshot = self.snapshot(image, "hrms", ["erpnext", "hrms"])

		self.assertEqual(
			self.site_changes(PilotImageSnapshot.run_app_prerequisite, image, snapshot),
			[("install", ["erpnext", "hrms"]), ("verify", ["frappe", "erpnext", "hrms"])],
		)

	def test_version_16_uninstalls_the_apps_after_the_photograph(self):
		image = self.image()
		snapshot = self.snapshot(image, "hrms", ["erpnext", "hrms"])

		self.assertEqual(
			self.site_changes(PilotImageSnapshot.run_app_post_requisite, image, snapshot),
			[("uninstall", ["erpnext", "hrms"]), ("verify", ["frappe"])],
		)

	def test_develop_installs_every_app_and_disables_the_others(self):
		"""Develop can turn an app off, so every snapshot shares one install of each app."""
		image = self.image("17.0.0-dev")
		self.snapshot(image, "crm", ["crm"])
		snapshot = self.snapshot(image, "hrms", ["erpnext", "hrms"])

		self.assertEqual(
			self.site_changes(PilotImageSnapshot.run_app_prerequisite, image, snapshot),
			[("install", ["crm", "erpnext", "hrms"]), ("disable", ["crm"])],
		)

	def test_develop_disables_the_apps_after_the_photograph(self):
		image = self.image("17.0.0-dev")
		snapshot = self.snapshot(image, "hrms", ["erpnext", "hrms"])

		self.assertEqual(
			self.site_changes(PilotImageSnapshot.run_app_post_requisite, image, snapshot),
			[("disable", ["erpnext", "hrms"])],
		)

	def test_a_snapshot_without_a_signup_app_leaves_the_site_alone(self):
		"""A Site image pins apps only to fetch them onto the bench."""
		image = self.image(image_type="Site")
		snapshot = self.snapshot(image, None, ["erpnext", "crm"])

		self.assertEqual(self.site_changes(PilotImageSnapshot.run_app_prerequisite, image, snapshot), [])
		self.assertEqual(self.site_changes(PilotImageSnapshot.run_app_post_requisite, image, snapshot), [])

	def test_tags_name_the_site_and_the_signup_app(self):
		"""Central picks an image by its tags."""
		apps_image = self.image()
		site_image = self.image(image_type="Site")
		base_image = self.image(image_type="Base")

		apps_tags = self.snapshot(apps_image, "hrms", ["erpnext", "hrms"]).get_atlas_tags(apps_image)
		site_tags = self.snapshot(site_image, None, ["erpnext"]).get_atlas_tags(site_image)
		base_tags = self.snapshot(base_image, None, []).get_atlas_tags(base_image)

		self.assertEqual((apps_tags["has_site"], apps_tags["has_apps"], apps_tags["app"]), ("1", "1", "hrms"))
		self.assertEqual((site_tags["has_site"], site_tags["has_apps"]), ("1", "0"))
		self.assertNotIn("app", site_tags)
		self.assertEqual((base_tags["has_site"], base_tags["has_apps"]), ("0", "0"))
		self.assertEqual(apps_tags["frappe_version"], "version-16")

	def atlas_reports(self, snapshot: PilotImageSnapshot, image: dict) -> bool:
		with patch(f"{CONTROLLER}.AtlasClient") as atlas:
			atlas.from_settings.return_value.get_snapshot.return_value = image
			return snapshot.complete_if_available()

	def test_a_snapshot_atlas_is_still_making_waits(self):
		snapshot = self.snapshot(self.image(), "crm", ["crm"])

		self.assertFalse(self.atlas_reports(snapshot, {"status": "uploading"}))
		self.assertEqual(snapshot.status, "Snapshotting")

	def test_a_snapshot_atlas_made_is_available(self):
		snapshot = self.snapshot(self.image(), "crm", ["crm"])

		self.assertTrue(self.atlas_reports(snapshot, {"status": "available"}))
		self.assertEqual(snapshot.status, "Available")
		self.assertIsNotNone(snapshot.built_at)

	def test_a_snapshot_atlas_failed_raises_its_reason(self):
		snapshot = self.snapshot(self.image(), "crm", ["crm"])

		with self.assertRaisesRegex(frappe.ValidationError, "disk full"):
			self.atlas_reports(snapshot, {"status": "failed", "transfer_error": "disk full"})

	def delete_at_atlas(self, snapshot: PilotImageSnapshot, delete=None, status=None) -> bool:
		"""Delete through an Atlas whose delete raises `delete`, then reports `status`."""
		client = MagicMock()
		client.delete_snapshot.side_effect = delete
		client.get_snapshot.return_value = {"status": status}

		return snapshot.delete_atlas_image(client)

	def test_an_image_atlas_is_deleting_lets_the_snapshot_go(self):
		snapshot = self.snapshot(self.image(), "crm", ["crm"])
		snapshot.db_set({"status": "Available", "error": "The build failed."})

		self.assertTrue(self.delete_at_atlas(snapshot, status="deleting"))
		self.assertEqual(snapshot.status, "Failed")
		self.assertIsNone(snapshot.snapshot_id)
		self.assertIn("The build failed.", snapshot.error)
		self.assertIn("cargo-snapshot/img-1 is deleted", snapshot.error)

	def test_an_image_atlas_no_longer_has_lets_the_snapshot_go(self):
		snapshot = self.snapshot(self.image(), "crm", ["crm"])

		self.assertTrue(self.delete_at_atlas(snapshot, delete=AtlasNotFound("gone")))
		self.assertIsNone(snapshot.snapshot_id)

	def test_a_refused_delete_keeps_the_snapshot_on_its_image(self):
		"""A later call tries again, so the id stays where it can find it."""
		snapshot = self.snapshot(self.image(), "crm", ["crm"])
		snapshot.db_set("status", "Available")

		self.assertFalse(self.delete_at_atlas(snapshot, delete=Exception("busy")))
		self.assertEqual(snapshot.status, "Available")
		self.assertEqual(snapshot.snapshot_id, "cargo-snapshot/img-1")

	def test_an_image_atlas_still_serves_keeps_the_snapshot_on_it(self):
		snapshot = self.snapshot(self.image(), "crm", ["crm"])

		self.assertFalse(self.delete_at_atlas(snapshot, status="available"))
		self.assertEqual(snapshot.snapshot_id, "cargo-snapshot/img-1")
