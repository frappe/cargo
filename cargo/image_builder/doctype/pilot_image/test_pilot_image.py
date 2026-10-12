# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.atlas_client import AtlasNotFound
from cargo.cargo.doctype.machine.machine import Machine as MachineDoc
from cargo.image_builder.doctype.pilot_image.apps import AppRelease
from cargo.image_builder.doctype.pilot_image.pilot_image import (
	PilotImage,
	retire_older_images,
	retry_failed_images,
	start_apps_images,
	start_base_images,
	start_site_images,
)
from cargo.testing import SETTINGS, set_frappe_branches, set_signup_apps, use_test_settings

CONTROLLER = "cargo.image_builder.doctype.pilot_image.pilot_image"
APPS = "cargo.image_builder.doctype.pilot_image.apps"
COMMIT = "a" * 40
NEW_COMMIT = "b" * 40
# What each app needs installed before it, as the marketplace declares it.
REQUIRES = {"hrms": ("erpnext",), "helpdesk": ("telephony",)}


def release(app: str, *_) -> AppRelease:
	"""A marketplace release that needs no network."""
	return AppRelease(
		name=app,
		version="1.0.0",
		repo=f"https://github.com/frappe/{app}",
		commit=f"{app}-commit",
		requires=REQUIRES.get(app, ()),
	)


class IntegrationTestPilotImage(IntegrationTestCase):
	"""How an image rents its machine, pins its apps, takes its snapshot and ends its build."""

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
		frappe_commit: str = COMMIT,
		signup_app: str = "",
	) -> PilotImage:
		"""An image with a running build machine, without asking Atlas for one."""
		if image_type == "Apps":
			signup_app = signup_app or "erpnext"
		with patch.object(PilotImage, "after_insert"):
			image: PilotImage = frappe.get_doc(
				{
					"doctype": "Pilot Image",
					"pilot_version": pilot_version or f"v0.0.1-{frappe.generate_hash(length=6)}",
					"frappe_branch": frappe_branch,
					"frappe_commit": frappe_commit,
					"image_type": image_type,
					"signup_app": signup_app,
					"app_commit": f"{signup_app}-commit" if signup_app else "",
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

	def pin_releases(self, image: PilotImage) -> list[str]:
		"""Pin the image's releases, returning the apps it installs in install order."""
		with (
			patch(f"{CONTROLLER}.get_frappe_release", return_value="16.35.0"),
			patch(f"{CONTROLLER}.get_release_at", side_effect=release),
			patch(f"{APPS}.get_latest_stable_release", side_effect=release),
		):
			image.pin_releases()

		return [app.app for app in image.apps]

	def completed(self, image: PilotImage, snapshot_id: str) -> PilotImage:
		image.db_set({"status": "Completed", "snapshot_id": snapshot_id})
		return image

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
		image = self.image("Base")
		machine = frappe.get_doc("Machine", image.machine)

		with (
			patch(f"{CONTROLLER}.base_image_id", return_value="img-1"),
			patch.object(MachineDoc, "request", return_value=machine) as request,
		):
			image.request_build_machine()

		self.assertEqual(request.call_args.kwargs["spec"].role, "builder")
		self.assertEqual(request.call_args.kwargs["base_image"], "img-1")
		self.assertEqual(image.status, "Provisioning")

	def test_a_site_image_boots_on_the_base_image_of_its_build(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		self.completed(self.image("Base", pilot_version=version), "img-base")
		# Another branch's Base image must not be picked.
		self.completed(self.image("Base", pilot_version=version, frappe_branch="develop"), "img-develop")
		# Nor the Base image of another Frappe commit.
		self.completed(self.image("Base", pilot_version=version, frappe_commit=NEW_COMMIT), "img-new-commit")

		self.assertEqual(self.image("Site", pilot_version=version).get_boot_image(), "img-base")

	def test_an_apps_image_boots_on_the_site_image_of_its_build(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		self.completed(self.image("Base", pilot_version=version), "img-base")
		self.completed(self.image("Site", pilot_version=version), "img-site")

		self.assertEqual(self.image("Apps", pilot_version=version).get_boot_image(), "img-site")

	def test_an_image_cannot_boot_before_the_image_under_it_completes(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		image = self.image("Site", pilot_version=version)

		with self.assertRaisesRegex(frappe.ValidationError, "has not built yet"):
			image.get_boot_image()

		self.image("Base", "Building", version)
		with self.assertRaisesRegex(frappe.ValidationError, "has not built yet"):
			image.get_boot_image()

	def test_base_and_site_images_install_no_apps(self):
		for image_type in ("Base", "Site"):
			with self.subTest(image_type):
				image = self.image(image_type)
				self.assertEqual(self.pin_releases(image), [])
				self.assertEqual(image.get_provision_environment()["REQUIRED_APPS"], "")

	def test_an_apps_image_installs_its_app_after_what_it_requires(self):
		with patch.dict(REQUIRES, {"raven": ("hrms",)}):
			apps = self.pin_releases(self.image("Apps", signup_app="raven"))

		self.assertEqual(apps, ["erpnext", "hrms", "raven"])

	def test_an_apps_image_installs_its_own_release(self):
		image = self.image("Apps", signup_app="helpdesk")
		self.pin_releases(image)

		self.assertEqual(
			image.get_provision_environment()["REQUIRED_APPS"].splitlines(),
			[
				"telephony https://github.com/frappe/telephony telephony-commit",
				"helpdesk https://github.com/frappe/helpdesk helpdesk-commit",
			],
		)

	def test_pinning_again_replaces_the_pins(self):
		"""A retried task pins the same apps, not twice as many."""
		image = self.image("Apps", signup_app="hrms")
		self.pin_releases(image)

		self.assertEqual(self.pin_releases(image), ["erpnext", "hrms"])

	def test_an_apps_image_needs_its_signup_app_and_release(self):
		with self.assertRaisesRegex(frappe.ValidationError, "need a signup app"):
			frappe.get_doc(
				{
					"doctype": "Pilot Image",
					"pilot_version": "v9.9.9",
					"frappe_branch": "version-16",
					"frappe_commit": COMMIT,
					"image_type": "Apps",
				}
			).insert()

	def test_the_provision_script_gets_the_region_and_the_release(self):
		image = self.image("Base")
		self.pin_releases(image)

		environment = image.get_provision_environment()

		self.assertEqual(environment["VERSION"], image.pilot_version)
		self.assertEqual(environment["FRAPPE_BRANCH"], "version-16")
		self.assertEqual(environment["FRAPPE_COMMIT"], COMMIT)
		self.assertEqual(environment["WILDCARD_DOMAIN"], SETTINGS["wildcard_domain"])
		self.assertEqual(environment["PROXY_SUBNET"], f"fdaa:{SETTINGS['region_id']:x}::/64")
		self.assertIn(f'["*.{SETTINGS["wildcard_domain"]}"]', environment["DOMAIN_PROVIDER"])
		self.assertNotIn("__WILDCARD_DOMAIN__", environment["DOMAIN_PROVIDER"])

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

	def test_a_running_build_is_asked_to_fail_and_loses_its_machine(self):
		"""The flag stops the next step. Terminating the machine ends the running one."""
		image = self.image(status="Building")
		workflow = self.workflow(image, "Running")

		with patch.object(MachineDoc, "terminate") as terminate:
			image.stop_build()

		terminate.assert_called_once()
		self.assertTrue(frappe.db.get_value("Press Workflow", workflow, "is_force_failure_requested"))

	def test_a_stopped_build_says_so_when_it_fails(self):
		"""The running step fails on a lost SSH session, which alone would read as a fault."""
		image = self.image(status="Building")
		workflow = frappe.get_doc("Press Workflow", self.workflow(image, "Failure"))
		workflow.is_force_failure_requested = 1

		with patch.object(MachineDoc, "terminate"), patch.object(PilotImage, "delete_atlas_image"):
			image.on_workflow_failure(workflow)

		self.assertEqual(image.status, "Failed")
		self.assertIn("Stopped by request", image.error)

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

		with (
			patch.object(PilotImage, "delete_atlas_image") as delete_atlas_image,
			patch.object(PilotImage, "release_build_machine") as release_build_machine,
			patch.object(PilotImage, "request_build_machine") as request_build_machine,
		):
			image.restart_build()

		delete_atlas_image.assert_called_once()
		release_build_machine.assert_called_once()
		request_build_machine.assert_called_once()
		self.assertIsNone(image.error)
		self.assertIsNone(image.build_log)

	def test_only_a_failed_build_can_be_restarted(self):
		with self.assertRaises(frappe.ValidationError):
			self.image(status="Building").restart_build()

	def test_a_failed_build_deletes_its_atlas_image(self):
		image = self.image(status="Snapshotting")
		workflow = frappe.get_doc("Press Workflow", self.workflow(image, "Failure"))

		with (
			patch.object(PilotImage, "release_build_machine") as release_build_machine,
			patch.object(PilotImage, "delete_atlas_image") as delete_atlas_image,
		):
			image.on_workflow_failure(workflow)

		self.assertEqual(image.status, "Failed")
		release_build_machine.assert_called_once()
		delete_atlas_image.assert_called_once()

	def test_a_finished_build_releases_its_machine(self):
		image = self.image(status="Snapshotting")
		image.db_set("error", "an earlier attempt")

		with patch.object(PilotImage, "release_build_machine") as release_build_machine:
			image.on_workflow_success(None)

		self.assertEqual(image.status, "Completed")
		self.assertIsNone(image.error)
		release_build_machine.assert_called_once()

	def atlas_reports(self, image: PilotImage, atlas_image: dict) -> MagicMock:
		with (
			patch(f"{CONTROLLER}.AtlasClient") as atlas,
			patch.object(PilotImage, "defer_current_task") as defer,
		):
			atlas.from_settings.return_value.get_snapshot.return_value = atlas_image
			image.wait_for_snapshot()

		return defer

	def test_an_image_atlas_is_still_making_is_waited_for(self):
		image = self.completed(self.image(), "img-1")

		self.atlas_reports(image, {"status": "uploading"}).assert_called_once()
		self.assertIsNone(image.built_at)

	def test_an_image_atlas_made_records_when(self):
		image = self.completed(self.image(), "img-1")

		self.atlas_reports(image, {"status": "available"}).assert_not_called()
		self.assertIsNotNone(image.built_at)

	def test_an_image_atlas_failed_raises_its_reason(self):
		image = self.completed(self.image(), "img-1")

		with self.assertRaisesRegex(frappe.ValidationError, "disk full"):
			self.atlas_reports(image, {"status": "failed", "transfer_error": "disk full"})

	def delete_at_atlas(self, image: PilotImage, delete=None, status=None):
		"""Delete through an Atlas whose delete raises `delete`, then reports `status`."""
		with patch(f"{CONTROLLER}.AtlasClient") as atlas:
			client = atlas.from_settings.return_value
			client.delete_snapshot.side_effect = delete
			client.get_snapshot.return_value = {"status": status}
			image.delete_atlas_image()

		return client

	def test_an_image_atlas_deletes_lets_its_snapshot_go(self):
		image = self.completed(self.image(), "img-1")

		self.delete_at_atlas(image, status="deleting")

		self.assertIsNone(image.snapshot_id)

	def test_an_image_atlas_no_longer_has_lets_its_snapshot_go(self):
		image = self.completed(self.image(), "img-1")

		self.delete_at_atlas(image, delete=AtlasNotFound("gone"))

		self.assertIsNone(image.snapshot_id)

	def test_an_image_atlas_still_serves_keeps_its_snapshot(self):
		"""A later call tries again, so the id stays where it can find it."""
		image = self.completed(self.image(), "img-1")

		with self.assertRaisesRegex(frappe.ValidationError, "img-1"):
			self.delete_at_atlas(image, status="available")

		self.assertEqual(frappe.db.get_value("Pilot Image", image.name, "snapshot_id"), "img-1")

	def test_protection_is_cleared_before_the_image_is_deleted(self):
		"""Atlas refuses to delete a protected System image."""
		client = self.delete_at_atlas(self.completed(self.image(), "img-1"), status="archived")

		self.assertEqual(
			[call[0] for call in client.method_calls[:2]],
			["set_image_termination_protection", "delete_snapshot"],
		)
		client.set_image_termination_protection.assert_called_once_with("img-1", enabled=False)

	def test_tags_name_the_site_and_the_signup_app(self):
		"""Central picks an image by its tags."""
		apps_image = self.image("Apps", signup_app="hrms")
		self.pin_releases(apps_image)

		apps_tags = apps_image.get_atlas_tags()
		site_tags = self.image("Site").get_atlas_tags()
		base_tags = self.image("Base").get_atlas_tags()

		self.assertEqual((apps_tags["has_site"], apps_tags["app"]), ("1", "hrms"))
		self.assertEqual((apps_tags["app_erpnext"], apps_tags["app_hrms"]), ("1.0.0", "1.0.0"))
		self.assertEqual(apps_tags["frappe_version"], "version-16")
		self.assertEqual(site_tags["has_site"], "1")
		self.assertNotIn("app", site_tags)
		self.assertEqual(base_tags["has_site"], "0")

	def retry_failed(self, fails: str | None = None) -> tuple[list[str], list[str]]:
		"""Run the retry without committing, returning what it restarted and what it asked to
		restart. The restart of image `fails` raises."""
		asked: list[str] = []

		def restart_build(image: PilotImage, automatic: bool = False) -> None:
			asked.append(image.name)
			if image.name == fails:
				raise frappe.ValidationError("Atlas is down")

		with (
			patch.object(PilotImage, "restart_build", autospec=True, side_effect=restart_build),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "rollback"),
		):
			return retry_failed_images(), asked

	def test_a_failed_image_type_of_the_latest_version_is_restarted(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		self.image("Base", "Completed", version)
		site = self.image("Site", "Failed", version)
		develop = self.image("Base", "Failed", version, frappe_branch="develop")

		restarted, _ = self.retry_failed()

		self.assertEqual(sorted(restarted), sorted([site.name, develop.name]))

	def test_a_build_in_progress_is_left_to_finish(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		self.image("Base", "Completed", version)
		self.image("Site", "Building", version)

		self.assertEqual(self.retry_failed()[0], [])

	def test_one_failed_restart_does_not_stop_the_others(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		self.image("Base", "Completed", version)
		site = self.image("Site", "Failed", version)
		develop = self.image("Base", "Failed", version, frappe_branch="develop")

		restarted, asked = self.retry_failed(fails=site.name)

		self.assertEqual(sorted(asked), sorted([site.name, develop.name]))
		self.assertEqual(restarted, [develop.name])

	def test_an_image_retried_three_times_is_left_failed(self):
		"""A build that fails the same way every time would rent a machine every minute."""
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		self.image("Site", "Failed", version).db_set("auto_retry_count", 3)
		develop = self.image("Base", "Failed", version, frappe_branch="develop")
		develop.db_set("auto_retry_count", 2)

		self.assertEqual(self.retry_failed()[0], [develop.name])

	def restart(self, retries: int, automatic: bool) -> PilotImage:
		image = self.image(status="Failed")
		image.db_set("auto_retry_count", retries)

		with (
			patch.object(PilotImage, "delete_atlas_image"),
			patch.object(PilotImage, "release_build_machine"),
			patch.object(PilotImage, "request_build_machine"),
		):
			image.restart_build(automatic=automatic)

		return image

	def test_an_automatic_restart_is_counted(self):
		self.assertEqual(self.restart(retries=1, automatic=True).auto_retry_count, 2)

	def test_a_restart_from_the_desk_leaves_the_retries_alone(self):
		"""The scheduler gave up on it. An operator can still restart it, and only setting the
		count lets the scheduler try it again."""
		self.assertEqual(self.restart(retries=3, automatic=False).auto_retry_count, 3)

	def test_the_retries_used_can_be_set_back(self):
		image = self.image(status="Failed")
		image.db_set("auto_retry_count", 3)

		image.set_auto_retry_count(0)

		self.assertEqual(frappe.db.get_value("Pilot Image", image.name, "auto_retry_count"), 0)

	def test_the_retries_used_cannot_go_below_zero(self):
		with self.assertRaises(frappe.ValidationError):
			self.image(status="Failed").set_auto_retry_count(-1)

	def start_latest_releases(
		self, pilot_version: str, heads: dict[str, str] | None = None, tracking: int = 1
	) -> list[str]:
		"""Start builds of `pilot_version` on each branch's head commit, without renting machines."""
		heads = heads or {}
		frappe.db.set_single_value("Cargo Settings", "track_pilot_releases", tracking)
		with (
			patch(f"{CONTROLLER}.get_latest_pilot_release", return_value=pilot_version),
			patch(
				f"{CONTROLLER}.get_frappe_head_commit", side_effect=lambda branch: heads.get(branch, COMMIT)
			),
			patch.object(PilotImage, "after_insert"),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "rollback"),
		):
			return start_base_images()

	def start_sites(self, tracking: int = 1) -> list[str]:
		frappe.db.set_single_value("Cargo Settings", "track_pilot_releases", tracking)
		with (
			patch.object(PilotImage, "after_insert"),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "rollback"),
		):
			return start_site_images()

	def built_on(self, names: list[str]) -> list[tuple]:
		return sorted(
			frappe.db.get_value("Pilot Image", name, ["image_type", "frappe_branch", "frappe_commit"])
			for name in names
		)

	def test_an_image_cannot_use_a_branch_settings_do_not_list(self):
		with self.assertRaises(frappe.ValidationError):
			self.image("Base", frappe_branch="version-15")

	def test_a_base_image_without_a_commit_takes_the_branch_head(self):
		with (
			patch(f"{CONTROLLER}.get_frappe_head_commit", return_value=NEW_COMMIT),
			patch.object(PilotImage, "after_insert"),
		):
			image = frappe.get_doc(
				{
					"doctype": "Pilot Image",
					"pilot_version": "v9.9.9",
					"frappe_branch": "develop",
					"image_type": "Base",
				}
			).insert()

		self.assertEqual(image.frappe_commit, NEW_COMMIT)

	def test_a_site_image_needs_a_commit(self):
		with self.assertRaisesRegex(frappe.ValidationError, "need the Frappe commit"):
			self.image("Site", frappe_commit="")

	def test_a_new_release_starts_base_images_on_each_branch_head(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"

		started = self.start_latest_releases(version, {"develop": NEW_COMMIT})

		self.assertEqual(
			self.built_on(started), [("Base", "develop", NEW_COMMIT), ("Base", "version-16", COMMIT)]
		)

	def test_a_new_release_starts_base_images_for_the_listed_branches_only(self):
		set_frappe_branches(("develop",))
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"

		self.start_latest_releases(version)

		self.assertEqual(
			frappe.get_all("Pilot Image", {"pilot_version": version}, pluck="frappe_branch"), ["develop"]
		)

	def test_a_new_frappe_commit_starts_a_base_image_of_the_same_release(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		self.image("Base", "Completed", version)
		self.image("Base", "Completed", version, frappe_branch="develop")

		started = self.start_latest_releases(version, {"version-16": NEW_COMMIT})

		self.assertEqual(self.built_on(started), [("Base", "version-16", NEW_COMMIT)])

	def test_a_build_that_already_has_a_base_image_is_not_started_again(self):
		"""Retrying a failed one is the retry job's work."""
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		self.image("Base", "Failed", version)
		self.image("Base", "Completed", version, frappe_branch="develop")

		self.assertEqual(self.start_latest_releases(version), [])

	def test_a_base_image_another_run_started_first_is_skipped(self):
		"""Both runs saw no image. The index refuses the second before it rents a machine."""
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		first = self.image("Base", "Provisioning", version)
		checked = frappe.db.exists

		def exists_before_the_other_run(doctype, filters=None, *args, **kwargs):
			if doctype == "Pilot Image" and isinstance(filters, dict) and "status" not in filters:
				return None
			return checked(doctype, filters, *args, **kwargs)

		with patch.object(frappe.db, "exists", side_effect=exists_before_the_other_run):
			started = self.start_latest_releases(version)

		self.assertEqual(self.built_on(started), [("Base", "develop", COMMIT)])
		self.assertNotIn(first.name, started)

	def test_a_build_takes_each_image_type_once(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		self.image("Site", "Failed", version)

		with self.assertRaises(frappe.UniqueValidationError):
			self.image("Site", "Provisioning", version)

	def test_no_release_is_built_while_tracking_is_off(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"

		self.assertEqual(self.start_latest_releases(version, tracking=0), [])
		self.assertEqual(self.start_sites(tracking=0), [])
		self.assertFalse(frappe.db.exists("Pilot Image", {"pilot_version": version}))

	def test_a_site_image_starts_once_its_base_image_completes(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		self.image("Base", "Completed", version, frappe_commit=NEW_COMMIT)
		self.image("Base", "Building", version, frappe_branch="develop")

		started = self.start_sites()

		self.assertEqual(self.built_on(started), [("Site", "version-16", NEW_COMMIT)])
		self.assertEqual(frappe.db.get_value("Pilot Image", started[0], "pilot_version"), version)

	def test_a_base_image_with_a_site_image_gets_no_other(self):
		version = f"v9.9.9-{frappe.generate_hash(length=6)}"
		self.image("Base", "Completed", version)
		self.image("Site", "Failed", version)

		self.assertEqual(self.start_sites(), [])

	def start_apps(self, releases: dict[str, str | None] | None = None) -> list[str]:
		"""Start Apps images, where `releases` maps an app to its newest stable commit, or to None
		when it has no stable release."""
		releases = releases or {}

		def newest_stable(app: str, frappe_version: str) -> AppRelease | None:
			commit = releases.get(app, f"{app}-commit")
			return commit and AppRelease(app, "1.0.0", f"https://github.com/frappe/{app}", commit, ())

		frappe.db.set_single_value("Cargo Settings", "track_pilot_releases", 1)
		with (
			patch(f"{CONTROLLER}.get_latest_stable_release", side_effect=newest_stable),
			patch.object(PilotImage, "after_insert"),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "rollback"),
		):
			return start_apps_images()

	def completed_site(self, frappe_branch: str = "version-16", frappe_commit: str = COMMIT) -> PilotImage:
		site = self.image("Site", "Completed", frappe_branch=frappe_branch, frappe_commit=frappe_commit)
		site.db_set("frappe_version", "16.35.0")
		return site

	def test_each_signup_app_gets_an_apps_image_on_the_newest_site_image(self):
		set_signup_apps(("erpnext", "helpdesk"))
		self.completed_site(frappe_commit=NEW_COMMIT)

		started = self.start_apps()

		self.assertEqual(
			sorted(
				frappe.db.get_value("Pilot Image", name, ["signup_app", "app_commit", "frappe_commit"])
				for name in started
			),
			[("erpnext", "erpnext-commit", NEW_COMMIT), ("helpdesk", "helpdesk-commit", NEW_COMMIT)],
		)

	def test_an_app_without_a_stable_release_gets_no_image(self):
		set_signup_apps(("erpnext", "helpdesk"))
		self.completed_site()

		started = self.start_apps({"helpdesk": None})

		self.assertEqual(
			[frappe.db.get_value("Pilot Image", name, "signup_app") for name in started], ["erpnext"]
		)

	def test_a_newer_site_image_alone_does_not_rebuild_an_app(self):
		set_signup_apps(("erpnext",))
		self.image("Apps", "Completed", signup_app="erpnext")
		self.completed_site(frappe_commit=NEW_COMMIT)

		self.assertEqual(self.start_apps(), [])

	def test_a_new_app_release_builds_on_the_newest_site_image(self):
		set_signup_apps(("erpnext",))
		self.image("Apps", "Completed", signup_app="erpnext")
		self.completed_site(frappe_commit=NEW_COMMIT)

		started = self.start_apps({"erpnext": "erpnext-new-commit"})

		self.assertEqual(
			frappe.db.get_value("Pilot Image", started[0], ["app_commit", "frappe_commit"]),
			("erpnext-new-commit", NEW_COMMIT),
		)

	def test_no_apps_image_starts_before_a_site_image_completes(self):
		self.image("Site", "Building")

		self.assertEqual(self.start_apps(), [])

	def test_the_newest_apps_image_of_each_app_is_retried(self):
		set_signup_apps(("erpnext",))
		older = self.image("Apps", "Failed", signup_app="erpnext")
		frappe.db.set_value("Pilot Image", older.name, "creation", "2000-01-01", update_modified=False)
		newest = self.image("Apps", "Failed", signup_app="erpnext", frappe_commit=NEW_COMMIT)

		restarted, _ = self.retry_failed()

		self.assertIn(newest.name, restarted)
		self.assertNotIn(older.name, restarted)

	def apps_release(self, day: int, status: str = "Completed") -> PilotImage:
		"""An Apps image of erpnext's own release, made on `day` of a month no other test reaches."""
		image = self.image("Apps", status, frappe_commit=f"{day:040d}", signup_app="erpnext")
		frappe.db.set_value(
			"Pilot Image", image.name, "creation", f"2099-01-{day:02d}", update_modified=False
		)
		return image

	def test_each_app_keeps_its_three_newest_images(self):
		set_signup_apps(("erpnext",))
		oldest = self.apps_release(1)
		newer = [self.apps_release(day) for day in (2, 3, 4)]

		asked = self.retire_older()

		self.assertIn(oldest.name, asked)
		for image in newer:
			self.assertNotIn(image.name, asked)

	def test_retiring_an_old_build_keeps_the_apps_images_built_on_it(self):
		"""An Apps image is a full disk, so it outlives the Site image it booted on."""
		oldest = self.release(1)
		apps = self.image("Apps", "Completed", oldest.pilot_version, signup_app="erpnext")
		[self.release(day) for day in (2, 3, 4)]

		asked = self.retire_older()

		self.assertIn(oldest.name, asked)
		self.assertNotIn(apps.name, asked)

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

	def test_each_frappe_branch_keeps_its_own_three_newest_builds(self):
		oldest = self.release(1)
		[self.release(day) for day in (2, 3, 4)]
		develop = self.image("Base", "Completed", frappe_branch="develop")
		frappe.db.set_value("Pilot Image", develop.name, "creation", "2099-01-01", update_modified=False)

		asked = self.retire_older()

		self.assertIn(oldest.name, asked)
		self.assertNotIn(develop.name, asked)

	def test_a_new_frappe_commit_counts_as_a_newer_build_of_the_same_release(self):
		oldest = self.release(1)
		for day, commit in ((2, NEW_COMMIT), (3, "c" * 40), (4, "d" * 40)):
			image = self.image("Base", "Completed", oldest.pilot_version, frappe_commit=commit)
			frappe.db.set_value(
				"Pilot Image", image.name, "creation", f"2099-01-{day:02d}", update_modified=False
			)

		self.assertIn(oldest.name, self.retire_older())

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

		with patch.object(PilotImage, "delete_atlas_image") as delete_atlas_image:
			image.retire()

		delete_atlas_image.assert_called_once()
		self.assertEqual(image.status, "Retired")

	def test_an_image_atlas_keeps_is_not_retired(self):
		image = self.image(status="Completed")

		with (
			patch.object(PilotImage, "delete_atlas_image", side_effect=frappe.ValidationError("kept")),
			self.assertRaises(frappe.ValidationError),
		):
			image.retire()

		self.assertEqual(frappe.db.get_value("Pilot Image", image.name, "status"), "Completed")

	def test_a_build_in_progress_cannot_be_retired(self):
		with self.assertRaises(frappe.ValidationError):
			self.image(status="Building").retire()

	def test_a_release_none_of_whose_builds_finished_is_retried(self):
		"""The newest release, not the newest that built, is the one left to recover."""
		self.release(1)
		unbuilt = self.release(2, status="Failed")

		restarted, _ = self.retry_failed()

		self.assertEqual(restarted, [unbuilt.name])

	def later_image_of(self, release: PilotImage, day: int, image_type: str) -> PilotImage:
		"""Another image of an existing release, made by hand on a later `day`."""
		image = self.image(image_type, "Failed", release.pilot_version)
		frappe.db.set_value(
			"Pilot Image", image.name, "creation", f"2099-01-{day:02d}", update_modified=False
		)

		return image

	def test_building_an_older_release_again_does_not_make_it_the_newest(self):
		"""A release is placed by its first image, so the newest release is still retried."""
		older = self.release(1)
		unbuilt = self.release(2, status="Failed")
		self.later_image_of(older, 3, "Site")

		restarted, _ = self.retry_failed()

		self.assertEqual(restarted, [unbuilt.name])

	def test_an_older_release_built_again_is_still_retired(self):
		oldest = self.release(1)
		[self.release(day) for day in (2, 3, 4)]
		rebuilt = self.later_image_of(oldest, 5, "Site")

		asked = self.retire_older()

		self.assertIn(oldest.name, asked)
		self.assertIn(rebuilt.name, asked)
