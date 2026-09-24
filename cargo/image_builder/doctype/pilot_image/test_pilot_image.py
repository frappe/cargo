# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.cargo.doctype.machine.machine import Machine as MachineDoc
from cargo.image_builder.doctype.pilot_image.apps import AppRelease
from cargo.image_builder.doctype.pilot_image.pilot_image import (
	PilotImage,
	retire_older_images,
	retry_failed_image_types_with_latest_version,
	start_image_build_with_latest_pilot_release,
)
from cargo.testing import SETTINGS, use_test_settings

CONTROLLER = "cargo.image_builder.doctype.pilot_image.pilot_image"
SNAPSHOT_CONTROLLER = "cargo.image_builder.doctype.pilot_image_snapshot.pilot_image_snapshot"
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
		# The jobs under test read every image, so one test's images must not reach the next.
		frappe.db.savepoint("pilot_image_test")
		self.addCleanup(frappe.db.rollback, save_point="pilot_image_test")
		use_test_settings()

	def image(
		self,
		image_type: str = "Site",
		status: str = "Provisioning",
		pilot_version: str | None = None,
		frappe_branch: str = "version-16",
	) -> PilotImage:
		"""An image with a running build machine, without asking Atlas for one."""
		with patch.object(PilotImage, "after_insert"):
			image: PilotImage = frappe.get_doc(
				{
					"doctype": "Pilot Image",
					"pilot_version": pilot_version or f"v0.0.1-{frappe.generate_hash(length=6)}",
					"frappe_branch": frappe_branch,
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
			patch(f"{CONTROLLER}.get_frappe_release", return_value="16.35.0"),
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
		for snapshot in (snapshotting, pending):
			self.assertEqual(frappe.db.get_value("Pilot Image Snapshot", snapshot, "status"), "Failed")
		self.assertIn("Not taken", frappe.db.get_value("Pilot Image Snapshot", pending, "error"))
		# Its image is deleted next, and only that marks it Failed.
		self.assertEqual(frappe.db.get_value("Pilot Image Snapshot", available, "status"), "Available")
		release_build_machine.assert_called_once()
		delete_atlas_images.assert_called_once()

	def test_a_snapshot_stays_available_until_atlas_deletes_its_image(self):
		"""Its record must not claim an image is gone that Atlas still serves."""
		image = self.image(status="Snapshotting")
		available = self.snapshot(image, "Available", "img-1")
		workflow = frappe.get_doc("Press Workflow", self.workflow(image, "Failure"))

		with (
			patch.object(PilotImage, "release_build_machine"),
			patch(f"{CONTROLLER}.AtlasClient"),
			patch(f"{SNAPSHOT_CONTROLLER}.PilotImageSnapshot.delete_atlas_image", return_value=False),
			self.assertRaises(frappe.ValidationError),
		):
			image.on_workflow_failure(workflow)

		status, error = frappe.db.get_value("Pilot Image Snapshot", available, ["status", "error"])
		self.assertEqual(status, "Available")
		self.assertIn("build failed", error)

	def test_a_finished_build_releases_its_machine(self):
		image = self.image(status="Snapshotting")
		image.db_set("error", "an earlier attempt")

		with patch.object(PilotImage, "release_build_machine") as release_build_machine:
			image.on_workflow_success(None)

		self.assertEqual(image.status, "Completed")
		self.assertIsNone(image.error)
		release_build_machine.assert_called_once()

	def test_every_image_is_tried_before_the_kept_ones_are_raised(self):
		image = self.image(status="Failed")
		self.snapshot(image, "Failed", "img-1")
		self.snapshot(image, "Available", "img-2")
		self.snapshot(image, "Failed")

		with (
			patch(f"{CONTROLLER}.AtlasClient"),
			# Atlas keeps img-1 and lets img-2 go, whichever it is asked about first.
			patch(
				f"{SNAPSHOT_CONTROLLER}.PilotImageSnapshot.delete_atlas_image",
				autospec=True,
				side_effect=lambda snapshot, client: snapshot.snapshot_id != "img-1",
			) as delete_atlas_image,
			self.assertRaisesRegex(frappe.ValidationError, "img-1"),
		):
			image.delete_atlas_images()

		self.assertEqual(delete_atlas_image.call_count, 2)

	def retry_failed_images(self, fails: str | None = None) -> tuple[list[str], list[str]]:
		"""Run the retry without committing, returning what it restarted and what it asked to
		restart. The restart of image `fails` raises."""
		asked: list[str] = []

		def restart_build(image: PilotImage) -> None:
			asked.append(image.name)
			if image.name == fails:
				raise frappe.ValidationError("Atlas is down")

		with (
			patch.object(PilotImage, "restart_build", autospec=True, side_effect=restart_build),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "rollback"),
		):
			return retry_failed_image_types_with_latest_version(), asked

	def test_a_failed_image_type_of_the_latest_version_is_restarted(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		self.image("Base", "Completed", version)
		site = self.image("Site", "Failed", version)
		apps = self.image("Apps", "Failed", version, frappe_branch="develop")

		restarted, _ = self.retry_failed_images()

		self.assertEqual(sorted(restarted), sorted([site.name, apps.name]))

	def test_a_build_in_progress_is_left_to_finish(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		self.image("Base", "Completed", version)
		self.image("Site", "Building", version)

		self.assertEqual(self.retry_failed_images()[0], [])

	def test_one_failed_restart_does_not_stop_the_others(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		self.image("Base", "Completed", version)
		site = self.image("Site", "Failed", version)
		apps = self.image("Apps", "Failed", version)

		restarted, asked = self.retry_failed_images(fails=site.name)

		self.assertEqual(sorted(asked), sorted([site.name, apps.name]))
		self.assertEqual(restarted, [apps.name])

	def start_latest_release(self, pilot_version: str, tracking: int = 1) -> list[str]:
		"""Start builds of `pilot_version` as the newest release, without renting machines."""
		frappe.db.set_single_value("Cargo Settings", "track_pilot_releases", tracking)
		with (
			patch(f"{CONTROLLER}.get_latest_pilot_release", return_value=pilot_version),
			patch.object(PilotImage, "after_insert"),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "rollback"),
		):
			return start_image_build_with_latest_pilot_release()

	def test_a_new_release_is_built_as_every_variant(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"

		started = self.start_latest_release(version)

		variants = frappe.get_all(
			"Pilot Image", {"pilot_version": version}, ["name", "image_type", "frappe_branch"]
		)
		self.assertEqual(sorted(started), sorted(variant.name for variant in variants))
		self.assertEqual(
			sorted((variant.image_type, variant.frappe_branch) for variant in variants),
			sorted(
				(image_type, frappe_branch)
				for image_type in ("Base", "Site", "Apps")
				for frappe_branch in ("version-16", "develop")
			),
		)

	def test_a_release_that_has_built_starts_nothing(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		self.image("Base", "Completed", version)

		self.assertEqual(self.start_latest_release(version), [])

	def test_a_variant_that_already_has_a_build_is_not_started_again(self):
		"""Retrying it is the retry job's work."""
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		failed = self.image("Site", "Failed", version)

		started = self.start_latest_release(version)

		self.assertEqual(len(started), 5)
		self.assertNotIn(failed.name, started)
		self.assertEqual(
			frappe.db.count(
				"Pilot Image", {"pilot_version": version, "image_type": "Site", "frappe_branch": "version-16"}
			),
			1,
		)

	def test_a_release_takes_each_variant_once(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		self.image("Site", "Failed", version)

		with self.assertRaises(frappe.UniqueValidationError):
			self.image("Site", "Provisioning", version)

	def test_a_variant_another_run_started_first_is_skipped(self):
		"""Both runs saw no image. The index refuses the second before it rents a machine."""
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		first = self.image("Site", "Provisioning", version)
		checked = frappe.db.exists

		def exists_before_the_other_run(doctype, filters=None, *args, **kwargs):
			if doctype == "Pilot Image" and isinstance(filters, dict) and "status" not in filters:
				return None
			return checked(doctype, filters, *args, **kwargs)

		with patch.object(frappe.db, "exists", side_effect=exists_before_the_other_run):
			started = self.start_latest_release(version)

		self.assertEqual(len(started), 5)
		self.assertNotIn(first.name, started)

	def test_no_release_is_built_while_tracking_is_off(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"

		self.assertEqual(self.start_latest_release(version, tracking=0), [])
		self.assertFalse(frappe.db.exists("Pilot Image", {"pilot_version": version}))

	def release(self, day: int, status: str = "Completed", image_type: str = "Base") -> PilotImage:
		"""An image of its own Pilot release, made on `day` of a month no other test reaches."""
		image = self.image(image_type, status, f"v9.9.{day}-{frappe.generate_hash(length=6)}")
		frappe.db.set_value(
			"Pilot Image", image.name, "creation", f"2099-01-{day:02d}", update_modified=False
		)

		return image

	def retire_older(self) -> list[str]:
		"""Run the retire job, recording what it asks to retire instead of deleting anything."""
		asked: list[str] = []
		with (
			patch.object(
				PilotImage, "retire", autospec=True, side_effect=lambda image: asked.append(image.name)
			),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "rollback"),
		):
			retire_older_images()

		return asked

	def test_only_the_three_newest_releases_are_kept(self):
		oldest = self.release(1)
		newer = [self.release(day) for day in (2, 3, 4)]

		asked = self.retire_older()

		self.assertIn(oldest.name, asked)
		for image in newer:
			self.assertNotIn(image.name, asked)

	def test_a_newer_release_that_has_not_built_is_not_retired(self):
		"""It is still being retried, and does not count as one of the three."""
		oldest = self.release(1)
		[self.release(day) for day in (2, 3, 4)]
		unbuilt = self.release(5, status="Failed")

		asked = self.retire_older()

		self.assertIn(oldest.name, asked)
		self.assertNotIn(unbuilt.name, asked)

	def test_an_old_build_in_progress_is_not_retired(self):
		building = self.release(1, status="Building")
		[self.release(day) for day in (2, 3, 4)]

		self.assertNotIn(building.name, self.retire_older())

	def test_retiring_deletes_the_atlas_images_first(self):
		image = self.image(status="Completed")

		with patch.object(PilotImage, "delete_atlas_images") as delete_atlas_images:
			image.retire()

		delete_atlas_images.assert_called_once()
		self.assertEqual(image.status, "Retired")

	def test_an_image_atlas_keeps_is_not_retired(self):
		image = self.image(status="Completed")

		with (
			patch.object(PilotImage, "delete_atlas_images", side_effect=frappe.ValidationError("kept")),
			self.assertRaises(frappe.ValidationError),
		):
			image.retire()

		self.assertEqual(frappe.db.get_value("Pilot Image", image.name, "status"), "Completed")

	def test_a_build_in_progress_cannot_be_retired(self):
		with self.assertRaises(frappe.ValidationError):
			self.image(status="Building").retire()
