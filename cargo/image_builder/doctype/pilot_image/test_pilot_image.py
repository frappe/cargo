# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.atlas_client import AtlasNotFound
from cargo.cargo.doctype.machine.machine import Machine as MachineDoc
from cargo.image_builder.doctype.pilot_image.apps import AppRelease
from cargo.image_builder.doctype.pilot_image.pilot_image import PilotImage
from cargo.testing import SETTINGS, use_test_settings

CONTROLLER = "cargo.image_builder.doctype.pilot_image.pilot_image"
# What each app needs installed before it, as the marketplace declares it.
REQUIRES = {"hrms": ("erpnext",), "helpdesk": ("telephony",)}


def release(app: str, frappe_version: str) -> AppRelease:
	"""A marketplace release that needs no network."""
	return AppRelease(
		name=app,
		version="1.0.0",
		repo=f"https://github.com/frappe/{app}",
		commit=f"{app}-commit",
		requires=REQUIRES.get(app, ()),
	)


class IntegrationTestPilotImage(IntegrationTestCase):
	"""How an image rents its machine, plans its snapshots and ends its build."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()

	def image(self, image_type: str = "Site", status: str = "Provisioning") -> PilotImage:
		"""An image with a running build machine, without asking Atlas for one."""
		with patch.object(PilotImage, "after_insert"):
			image: PilotImage = frappe.get_doc(
				{
					"doctype": "Pilot Image",
					"pilot_version": f"v0.0.1-{frappe.generate_hash(length=6)}",
					"frappe_branch": "version-16",
					"image_type": image_type,
				}
			).insert()

		image.db_set({"machine": self.machine(image), "status": status})
		image.reload()

		return image

	def machine(self, image: PilotImage, status: str = "Running") -> str:
		return (
			frappe.get_doc(
				{
					"doctype": "Machine",
					"reference_doctype": image.doctype,
					"reference_name": image.name,
					"role": "builder",
					"disk_size_gb": 8,
					"vm_id": f"vm-{frappe.generate_hash(length=8)}",
					"address": "fdaa:1::1d",
					"status": status,
				}
			)
			.insert()
			.name
		)

	def create_snapshots(self, image: PilotImage) -> list[dict]:
		with (
			patch(f"{CONTROLLER}.frappe_release", return_value="16.35.0"),
			patch(f"{CONTROLLER}.get_compatible_app_commit", side_effect=release),
		):
			image.create_snapshots()

		snapshots = frappe.get_all(
			"Pilot Image Snapshot",
			filters={"pilot_image": image.name},
			fields=["name", "signup_app"],
			order_by="creation asc",
		)
		for snapshot in snapshots:
			snapshot.apps = [
				row.app for row in frappe.get_doc("Pilot Image Snapshot", snapshot.name).required_apps
			]

		return snapshots

	def snapshot(self, image: PilotImage, status: str, snapshot_id: str | None = None) -> str:
		return (
			frappe.get_doc(
				{
					"doctype": "Pilot Image Snapshot",
					"pilot_image": image.name,
					"status": status,
					"snapshot_id": snapshot_id,
				}
			)
			.insert()
			.name
		)

	def workflow(self, image: PilotImage, status: str) -> str:
		"""A build workflow linked to the image, without running it."""
		workflow = frappe.get_doc(
			{
				"doctype": "Press Workflow",
				"linked_doctype": image.doctype,
				"linked_docname": image.name,
				"main_method_name": "_create_image",
				"main_method_title": "Create Image",
				"status": status,
			}
		)
		with patch("cargo.workflow_engine.doctype.press_workflow.press_workflow.enqueue_workflow"):
			workflow.insert(ignore_permissions=True)

		return workflow.name

	def test_a_new_image_rents_a_builder_and_waits_for_it(self):
		image = self.image()
		machine = frappe.get_doc("Machine", image.machine)

		with (
			patch(f"{CONTROLLER}.base_image_id", return_value="img-1"),
			patch.object(MachineDoc, "request", return_value=machine) as request,
		):
			image.request_build_machine()

		self.assertEqual(request.call_args.kwargs["spec"].role, "builder")
		self.assertEqual(request.call_args.kwargs["base_image"], "img-1")
		self.assertEqual(image.status, "Provisioning")

	def test_a_base_image_takes_one_snapshot_with_no_apps(self):
		snapshots = self.create_snapshots(self.image("Base"))

		self.assertEqual([(snapshot.signup_app, snapshot.apps) for snapshot in snapshots], [(None, [])])

	def test_a_site_image_takes_one_snapshot_that_pins_every_app(self):
		"""Its site stays bare. The pins are what the bench fetches."""
		snapshots = self.create_snapshots(self.image("Site"))

		self.assertEqual(len(snapshots), 1)
		self.assertIsNone(snapshots[0].signup_app)
		self.assertEqual(snapshots[0].apps, ["erpnext", "crm", "hrms", "telephony", "helpdesk", "gameplan"])

	def test_an_apps_image_takes_a_snapshot_per_signup_app(self):
		snapshots = self.create_snapshots(self.image("Apps"))

		self.assertEqual(
			[(snapshot.signup_app, snapshot.apps) for snapshot in snapshots],
			[
				("erpnext", ["erpnext"]),
				("crm", ["crm"]),
				("hrms", ["erpnext", "hrms"]),
				("helpdesk", ["telephony", "helpdesk"]),
				("gameplan", ["gameplan"]),
			],
		)

	def test_snapshots_are_planned_once(self):
		"""A retried task finds the snapshots its first run made."""
		image = self.image("Apps")
		self.create_snapshots(image)

		self.assertEqual(len(self.create_snapshots(image)), 5)

	def test_an_app_whose_requirement_is_not_listed_with_it_is_refused(self):
		with patch.dict(REQUIRES, {"crm": ("erpnext",)}), self.assertRaises(frappe.ValidationError):
			self.create_snapshots(self.image("Apps"))

	def test_the_provision_script_gets_the_region_and_the_release(self):
		image = self.image("Base")
		self.create_snapshots(image)

		environment = image.get_provision_environment()

		self.assertEqual(environment["VERSION"], image.pilot_version)
		self.assertEqual(environment["FRAPPE_BRANCH"], "version-16")
		self.assertEqual(environment["WILDCARD_DOMAIN"], SETTINGS["wildcard_domain"])
		self.assertEqual(environment["PROXY_SUBNET"], f"fdaa:{SETTINGS['region_id']:x}::/64")
		self.assertIn(f'["*.{SETTINGS["wildcard_domain"]}"]', environment["DOMAIN_PROVIDER"])
		self.assertNotIn("__WILDCARD_DOMAIN__", environment["DOMAIN_PROVIDER"])

	def test_a_base_image_fetches_no_apps(self):
		image = self.image("Base")
		self.create_snapshots(image)

		self.assertEqual(image.get_provision_environment()["REQUIRED_APPS"], "")

	def test_an_app_two_snapshots_share_is_fetched_once(self):
		image = self.image("Apps")
		self.create_snapshots(image)

		lines = image.get_provision_environment()["REQUIRED_APPS"].splitlines()

		self.assertEqual(len(lines), 6)
		self.assertEqual(lines[0], "https://github.com/frappe/erpnext erpnext-commit")

	def test_a_running_machine_starts_the_build(self):
		image = self.image()

		with patch.object(PilotImage, "create_image") as create_image:
			image.sync_machines()

		create_image.assert_called_once()
		self.assertEqual(image.status, "Building")

	def test_a_dead_machine_fails_the_image(self):
		image = self.image()
		frappe.db.set_value("Machine", image.machine, "status", "Broken")

		with patch.object(PilotImage, "create_image") as create_image:
			image.sync_machines()

		create_image.assert_not_called()
		self.assertEqual(image.status, "Failed")
		self.assertIn(image.machine, image.error)

	def test_a_booting_machine_leaves_the_image_waiting(self):
		image = self.image()
		frappe.db.set_value("Machine", image.machine, "status", "Pending")

		with patch.object(PilotImage, "create_image") as create_image:
			image.sync_machines()

		create_image.assert_not_called()
		self.assertEqual(image.status, "Provisioning")

	def test_a_running_build_is_asked_to_fail_itself(self):
		"""Its failure callback releases the machine, so nothing is torn down here."""
		image = self.image(status="Building")
		workflow = self.workflow(image, "Running")

		with patch.object(MachineDoc, "terminate") as terminate:
			image.stop_build()

		terminate.assert_not_called()
		self.assertTrue(frappe.db.get_value("Press Workflow", workflow, "is_force_failure_requested"))

	def test_a_build_waiting_on_its_success_callback_cannot_be_stopped(self):
		"""The callback records the result, so a stop here would race it."""
		image = self.image(status="Snapshotting")
		self.workflow(image, "Success")

		with patch.object(MachineDoc, "terminate") as terminate, self.assertRaises(frappe.ValidationError):
			image.stop_build()

		terminate.assert_not_called()
		self.assertEqual(frappe.db.get_value("Pilot Image", image.name, "status"), "Snapshotting")

	def test_a_build_with_no_workflow_releases_its_own_machine(self):
		"""A machine that is still booting has no workflow to fail."""
		image = self.image()

		with patch.object(MachineDoc, "terminate") as terminate:
			image.stop_build()

		terminate.assert_called_once()
		self.assertEqual(image.status, "Failed")
		self.assertIn("stopped by", image.error)

	def test_a_finished_build_cannot_be_stopped(self):
		with self.assertRaises(frappe.ValidationError):
			self.image(status="Completed").stop_build()

	def test_a_restart_starts_over_on_a_new_machine(self):
		image = self.image(status="Failed")
		image.db_set({"error": "boom", "build_log": "log"})
		self.snapshot(image, "Failed")

		with (
			patch.object(PilotImage, "delete_atlas_images") as delete_atlas_images,
			patch.object(PilotImage, "release_build_machine") as release_build_machine,
			patch.object(PilotImage, "request_build_machine") as request_build_machine,
		):
			image.restart_build()

		delete_atlas_images.assert_called_once()
		release_build_machine.assert_called_once()
		request_build_machine.assert_called_once()
		self.assertFalse(frappe.db.exists("Pilot Image Snapshot", {"pilot_image": image.name}))
		self.assertIsNone(image.error)
		self.assertIsNone(image.build_log)

	def test_only_a_failed_build_can_be_restarted(self):
		with self.assertRaises(frappe.ValidationError):
			self.image(status="Building").restart_build()

	def test_one_failed_snapshot_fails_the_whole_image(self):
		image = self.image(status="Snapshotting")
		available = self.snapshot(image, "Available", "img-1")
		snapshotting = self.snapshot(image, "Snapshotting", "img-2")
		pending = self.snapshot(image, "Pending")
		workflow = frappe.get_doc("Press Workflow", self.workflow(image, "Failure"))

		with (
			patch.object(PilotImage, "release_build_machine") as release_build_machine,
			patch.object(PilotImage, "delete_atlas_images") as delete_atlas_images,
		):
			image.on_workflow_failure(workflow)

		self.assertEqual(image.status, "Failed")
		for snapshot in (available, snapshotting, pending):
			self.assertEqual(frappe.db.get_value("Pilot Image Snapshot", snapshot, "status"), "Failed")
		self.assertIn("deleted", frappe.db.get_value("Pilot Image Snapshot", available, "error"))
		self.assertIn("Not taken", frappe.db.get_value("Pilot Image Snapshot", pending, "error"))
		release_build_machine.assert_called_once()
		delete_atlas_images.assert_called_once()

	def test_a_finished_build_releases_its_machine(self):
		image = self.image(status="Snapshotting")
		image.db_set("error", "an earlier attempt")

		with patch.object(PilotImage, "release_build_machine") as release_build_machine:
			image.on_workflow_success(None)

		self.assertEqual(image.status, "Completed")
		self.assertIsNone(image.error)
		release_build_machine.assert_called_once()

	def test_an_image_atlas_already_deleted_is_not_an_error(self):
		image = self.image(status="Failed")
		self.snapshot(image, "Failed", "img-1")

		with patch(f"{CONTROLLER}.AtlasClient") as atlas:
			atlas.from_settings.return_value.delete_snapshot.side_effect = AtlasNotFound("gone")
			image.delete_atlas_images()

	def test_every_image_is_tried_before_a_refusal_is_raised(self):
		image = self.image(status="Failed")
		self.snapshot(image, "Failed", "img-1")
		self.snapshot(image, "Failed", "img-2")
		self.snapshot(image, "Failed")

		with patch(f"{CONTROLLER}.AtlasClient") as atlas:
			delete_snapshot = atlas.from_settings.return_value.delete_snapshot
			delete_snapshot.side_effect = [Exception("busy"), None]
			with self.assertRaises(frappe.ValidationError):
				image.delete_atlas_images()

		self.assertEqual(delete_snapshot.call_count, 2)
