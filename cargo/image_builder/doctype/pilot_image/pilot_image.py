# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt
import json
import typing

import frappe
from frappe import _
from frappe.query_builder import Order
from frappe.query_builder.functions import Min
from frappe.utils import now_datetime

from cargo.atlas_client import PILOT_IMAGE_OS_TAGS, AtlasClient, AtlasNotFound, base_image_id
from cargo.cargo.doctype.machine.machine import DEAD_MACHINE_STATES
from cargo.cargo.doctype.machine.machine import Machine as MachineDoc
from cargo.image_builder.doctype.pilot_image.apps import (
	do_assets_exist,
	get_install_order,
	get_latest_stable_release,
	get_release_at,
	get_signup_apps,
)
from cargo.image_builder.doctype.pilot_image.builder import (
	PING_TIMEOUT,
	PROVISION_TIMEOUT,
	SSH_READY_TIMEOUT,
	Builder,
	get_build_spec,
)
from cargo.image_builder.doctype.pilot_image.releases import (
	get_frappe_head_commit,
	get_frappe_release,
	get_latest_pilot_release,
)
from cargo.ssh import OutputLog, script
from cargo.workflow_engine.doctype.press_workflow.decorators import flow, task
from cargo.workflow_engine.doctype.press_workflow.workflow_builder import WorkflowBuilder
from cargo.workflow_engine.utils import deadlock_retry

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings
	from cargo.image_builder.doctype.pilot_image.apps import AppRelease

# The task holds the wait for the machine as well as the script it then runs, so a slow
# boot cannot eat into the time the script is allowed.
BUILD_TIMEOUT = PING_TIMEOUT + SSH_READY_TIMEOUT + PROVISION_TIMEOUT
DOMAIN_PROVIDER = ("image_builder", "conf", "pilot", "domain_provider.py")


class PilotImage(WorkflowBuilder):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from cargo.image_builder.doctype.pilot_image_app.pilot_image_app import PilotImageApp

		app_commit: DF.Data
		apps: DF.Table[PilotImageApp]
		auto_retry_count: DF.Int
		build_log: DF.Code | None
		built_at: DF.Datetime | None
		error: DF.LongText | None
		frappe_branch: DF.Data
		frappe_commit: DF.Data
		frappe_version: DF.Data | None
		image_type: DF.Literal["Base", "Site", "Apps"]
		machine: DF.Link | None
		manually_failed: DF.Check
		pilot_version: DF.Data
		signup_app: DF.Data
		snapshot_id: DF.Data | None
		status: DF.Literal["Provisioning", "Building", "Snapshotting", "Completed", "Failed", "Retired"]
	# end: auto-generated types

	"""One build machine, provisioned and snapshotted into one Atlas image."""

	def before_insert(self) -> None:
		if self.frappe_branch not in get_frappe_branches():
			frappe.throw(_("Frappe branch {0} is not listed in Cargo Settings.").format(self.frappe_branch))

		if not self.frappe_commit:
			if self.image_type != "Base":
				frappe.throw(
					_("{0} images need the Frappe commit of the image they boot on.").format(self.image_type)
				)
			self.frappe_commit = get_frappe_head_commit(self.frappe_branch)

		if self.image_type == "Apps" and not (self.signup_app and self.app_commit):
			frappe.throw(_("Apps images need a signup app and its release commit."))

	def after_insert(self) -> None:
		"""Request a build machine from atlas."""
		self.request_build_machine()

	def request_build_machine(self) -> None:
		"""Rent a machine to build on. `sync_machines` starts the build once it is running."""
		machine = MachineDoc.request(owner=self, spec=get_build_spec(), base_image=self.get_boot_image()).name
		self.db_set({"machine": machine, "status": "Provisioning"})

	def get_boot_image(self) -> str:
		"""Base boots on Ubuntu, or on the branch's newest Base image when Cargo Settings updates
		Pilot in place. Site boots on the Base image of its build, and Apps on its Site image."""
		if self.image_type == "Base":
			return self.previous_base_image or base_image_id(minimal=True)

		boot_image_type = "Base" if self.image_type == "Site" else "Site"
		snapshot_id = frappe.db.get_value(
			"Pilot Image",
			{
				"pilot_version": self.pilot_version,
				"frappe_branch": self.frappe_branch,
				"frappe_commit": self.frappe_commit,
				"image_type": boot_image_type,
				"status": "Completed",
			},
			"snapshot_id",
		)

		if not snapshot_id:
			frappe.throw(
				_("The {0} image of Pilot {1} on Frappe {2} at {3} has not built yet.").format(
					boot_image_type, self.pilot_version, self.frappe_branch, self.frappe_commit[:7]
				)
			)

		return snapshot_id

	@property
	def build_machine(self) -> MachineDoc | None:
		if not self.machine:
			return None

		return frappe.get_doc("Machine", self.machine)

	@property
	def build_settings(self) -> "CargoSettings":
		return frappe.get_cached_doc("Cargo Settings")

	@property
	def previous_base_image(self) -> str | None:
		"""The snapshot of the branch's newest completed Base image, which may be of an older
		Pilot release or Frappe commit. None unless Cargo Settings updates Pilot in place."""
		if not self.build_settings.update_pilot_in_base_image:
			return None

		return frappe.db.get_value(
			"Pilot Image",
			{"image_type": "Base", "status": "Completed", "frappe_branch": self.frappe_branch},
			"snapshot_id",
			order_by="creation desc",
		)

	def sync_machines(self) -> None:
		"""Once the machine is live we can start the initial build process."""
		# Past this, the workflow owns the machine: a finished build terminates it on purpose.
		if self.status != "Provisioning":
			return

		machine_status = frappe.db.get_value("Machine", self.machine, "status")
		if machine_status in DEAD_MACHINE_STATES:
			self.status = "Failed"
			self.error = f"Machine {self.machine} is dead. Status: {machine_status}"
			self.save()
			return

		if machine_status == "Running":
			self.status = "Building"
			self.save()
			self.create_image()
			return

		# Any thing other than Dead and Running is a transient state, wait for it.
		return

	def create_image(self) -> None:
		"""Build the pilot image on the machine."""
		if self.status != "Building":
			frappe.throw(_("Cannot build a pilot image that is not in 'Building' status."))

		self._create_image.run_as_workflow()

	@frappe.whitelist()
	def stop_build(self) -> None:
		"""Stop the build."""
		if self.status == "Provisioning":
			self.status = "Failed"
			self.error = _("Build stopped by {0}.").format(frappe.session.user)
			self.manually_failed = True
			self.save()
			self.release_build_machine()
			return

		if self.status not in ("Building", "Snapshotting"):
			frappe.throw(_("Only a build in progress can be stopped."))

		workflow = frappe.db.get_value(
			"Press Workflow",
			{
				"linked_doctype": self.doctype,
				"linked_docname": self.name,
				"status": ("in", ("Queued", "Running")),
			},
		)
		if not workflow:
			# Finished, with its callback still to run. The callback records the result.
			frappe.throw(_("The build has already finished. Its result is being recorded."))

		frappe.get_doc("Press Workflow", workflow).force_fail()
		self.manually_failed = True
		self.save()
		self.release_build_machine()

	@frappe.whitelist()
	def restart_build(self, automatic: bool = False) -> None:
		"""Build this image again from the start, on a new machine. Only a restart by the
		scheduler counts towards its automatic retries."""
		if self.status != "Failed":
			frappe.throw(_("Cannot restart a build that is not in 'Failed' status."))

		self.delete_atlas_image()
		self.release_build_machine()

		self.apps = []
		self.built_at = None
		self.error = None
		self.build_log = None
		self.frappe_version = None
		self.manually_failed = False
		if automatic:
			self.auto_retry_count += 1
		self.save()
		self.request_build_machine()

	@frappe.whitelist()
	def set_auto_retry_count(self, count: int) -> None:
		"""Set how many automatic retries this image has used, such as 0 to let the scheduler
		retry it again."""
		if count < 0:
			frappe.throw(_("Automatic retries cannot be below 0."))

		self.auto_retry_count = count
		self.save()

	@frappe.whitelist()
	def build_apps_images(self) -> list[str]:
		"""Build each signup app's newest release on this Site image, even if it has an image elsewhere."""
		if self.image_type != "Site" or self.status != "Completed":
			frappe.throw(_("Apps images can only be built on a Completed Site image."))

		return start_apps_images_on(self, force=True)

	@flow
	def _create_image(self):
		# Pinned first: the provision script fetches the releases pinned here.
		self.pin_releases()
		self.run_provision_script()
		self.take_snapshot()
		self.wait_for_snapshot()

	@task(queue="short")
	@deadlock_retry(times=3)
	def pin_releases(self) -> None:
		"""Record the Frappe version, and the app releases an Apps image installs."""
		self.frappe_version = get_frappe_release(self.frappe_commit)

		self.apps = []
		if self.image_type == "Apps":
			release = get_release_at(self.signup_app, self.app_commit)
			for app in get_install_order(release, self.frappe_version):
				self.append(
					"apps",
					{
						"app": app.name,
						"version": app.version,
						"branch": app.branch,
						"commit": app.commit,
						"repo": app.repo,
					},
				)

		self.save()

	@task(queue="long", timeout=3600)
	def take_snapshot(self) -> None:
		"""Ask Atlas to photograph the build machine as it is now."""
		machine = self.build_machine
		Builder().flush_build_machine(machine.address, machine.get_password("ssh_private_key"))

		self.snapshot_id = machine.snapshot(self.name, self.get_atlas_tags())
		self.status = "Snapshotting"
		self.save()

	@task(queue="short")
	def wait_for_snapshot(self) -> None:
		"""Wait for Atlas to make the image. A deferred task is run again about once a minute."""
		atlas_image = AtlasClient.from_settings().get_snapshot(self.snapshot_id)
		status = atlas_image.get("status")
		if status in ("failed", "deleting", "archived"):
			frappe.throw(
				_("Atlas image {0} is {1}: {2}").format(
					self.snapshot_id, status, atlas_image.get("transfer_error") or _("Atlas gave no reason.")
				)
			)

		if status != "available":
			self.defer_current_task(_("Atlas is still making image {0}.").format(self.snapshot_id))
			return

		self.built_at = now_datetime()
		self.save()

	def get_atlas_tags(self) -> dict[str, str]:
		"""The image's specification, which is what a search at Atlas asks for."""
		tags = {
			"purpose": "pilot",
			"pilot_version": self.pilot_version,
			"frappe_version": self.frappe_branch,
			"has_site": str(int(self.image_type != "Base")),
			**PILOT_IMAGE_OS_TAGS,
		}
		if self.signup_app:
			tags["app"] = self.signup_app

		for app in self.apps:
			tags[f"app_{app.app}"] = app.version

		return tags

	@task(queue="long", timeout=BUILD_TIMEOUT)
	def run_provision_script(self) -> None:
		"""Based on the image type run the provision script on the machine."""
		machine: MachineDoc = self.build_machine
		private_key = machine.get_password("ssh_private_key")
		builder = Builder()
		builder.wait_until_reachable(machine.address, private_key)

		# Creating site is handled by the PilotImage doctype itself if required.
		with OutputLog(self, "build_log") as log:
			builder.run_provision_script_on_build_machine(
				machine.address,
				private_key,
				self.get_provision_environment(),
				on_output=log.write,
			)

	def get_provision_environment(self) -> dict[str, str]:
		"""What provision.sh reads. The bench, site and admin domain are the same for every
		image, so the script names those itself."""
		proxy_subnet = f"fdaa:{int(self.build_settings.region_id or 0):x}::/64"
		domain_provider = (
			script(*DOMAIN_PROVIDER)
			.replace('"__WILDCARD_DOMAIN__"', json.dumps(f"*.{self.build_settings.wildcard_domain}"))
			.replace('"__PROXY_SUBNET__"', json.dumps(proxy_subnet))
		)

		return {
			"VERSION": self.pilot_version,
			"FRAPPE_BRANCH": self.frappe_branch,
			"FRAPPE_COMMIT": self.frappe_commit,
			"WILDCARD_DOMAIN": self.build_settings.wildcard_domain or "",
			"PROXY_SUBNET": proxy_subnet,
			"DOMAIN_PROVIDER": domain_provider,
			"IMAGE_TYPE": self.image_type,
			"REQUIRED_APPS": "\n".join(
				f"{app.app} {app.repo} {app.branch} {app.commit}" for app in self.apps
			),
		}

	def on_workflow_failure(self, workflow) -> None:
		"""The workflow engine has given up on this image. The machine is still running, but
		the image is not going to be built."""
		failed = frappe.db.get_value(
			"Press Workflow Task",
			{"workflow": workflow.name, "status": "Failure"},
			["method_title", "traceback"],
			order_by="creation desc",
			as_dict=True,
		)
		stage = failed.method_title if failed else _("Build")
		if workflow.is_force_failure_requested:
			stage = _("Stopped by request during: {0}").format(stage)
		traceback = ((failed and failed.traceback) or workflow.workflow_traceback or "").strip()

		# Recorded before the machine goes, so a failed release cannot hide why the build failed.
		self.status = "Failed"
		self.error = f"{stage}\n{traceback}"
		self.save()

		self.release_build_machine()
		self.delete_atlas_image()

	def on_workflow_success(self, workflow) -> None:
		"""The workflow engine has finished this image. The machine is still running, but the
		image is built and its snapshot is available."""
		self.status = "Completed"
		self.error = None
		self.save()

		self.release_build_machine()

	def release_build_machine(self) -> None:
		"""Let the build machine go. A refusal leaves it Broken and in the Error Log, which
		`Machine.terminate` records."""
		machine = self.build_machine
		if machine and machine.status != "Terminated":
			machine.terminate()

	def delete_atlas_image(self) -> None:
		"""Delete the Atlas image this build took, so it cannot be booted. Throws while Atlas
		still has it, so the caller can try again."""
		if not self.snapshot_id:
			return

		client = AtlasClient.from_settings()
		try:
			# A Pilot snapshot is a System image, which Atlas protects from deletion.
			client.set_image_termination_protection(self.snapshot_id, enabled=False)
			client.delete_snapshot(self.snapshot_id)
			status = client.get_snapshot(self.snapshot_id).get("status")
		except AtlasNotFound:
			status = "deleted"

		# Atlas reclaims a deleting or archived image itself, and boots neither.
		if status not in ("deleted", "deleting", "archived"):
			frappe.throw(_("Atlas still has image {0}, which is {1}.").format(self.snapshot_id, status))

		self.db_set("snapshot_id", None)

	def retire(self) -> None:
		"""Delete this image's Atlas image and mark it Retired. Throws while Atlas keeps it,
		leaving the image as it was for a later run."""
		if self.status not in ("Completed", "Failed"):
			frappe.throw(_("Only a finished build can be retired."))

		self.delete_atlas_image()
		self.status = "Retired"
		self.save()


def get_frappe_branches() -> list[str]:
	"""The Frappe branches listed in Cargo Settings."""
	return frappe.get_all(
		"Frappe Branch",
		filters={"parenttype": "Cargo Settings", "parentfield": "frappe_branches"},
		pluck="branch",
		order_by="idx asc",
	)


def get_newest_completed_image(image_type: str, frappe_branch: str) -> dict | None:
	return frappe.db.get_value(
		"Pilot Image",
		{"image_type": image_type, "frappe_branch": frappe_branch, "status": "Completed"},
		["pilot_version", "frappe_branch", "frappe_commit", "frappe_version"],
		order_by="creation desc",
		as_dict=True,
	)


def get_builds(frappe_branch: str, completed_only: bool = False) -> list[dict]:
	"""The Pilot version and Frappe commit pairs of the Base and Site images on `frappe_branch`,
	newest first. A pair is placed by its first image, so building an older pair again does
	not make it the newest."""
	image = frappe.qb.DocType("Pilot Image")
	query = (
		frappe.qb.from_(image)
		.select(image.pilot_version, image.frappe_commit, Min(image.creation).as_("first_created"))
		.where(image.frappe_branch == frappe_branch)
		.where(image.image_type.isin(["Base", "Site"]))
		.groupby(image.pilot_version, image.frappe_commit)
		.orderby(Min(image.creation), order=Order.desc)
	)
	if completed_only:
		query = query.where(image.status == "Completed")

	return query.run(as_dict=True)


def is_tracking_releases() -> bool:
	return bool(frappe.db.get_single_value("Cargo Settings", "track_pilot_releases"))


def start_image(image: dict[str, str]) -> str | None:
	"""Insert `image`, which rents its build machine, unless it already has a build. Returns
	the image started."""
	# A failed build is retried by `retry_failed_images`.
	if frappe.db.exists("Pilot Image", image):
		return None

	try:
		name = frappe.get_doc({"doctype": "Pilot Image", **image}).insert().name
		# Inserting rents a machine at Atlas, which a later rollback cannot give back.
		frappe.db.commit()  # nosemgrep
	except frappe.UniqueValidationError:
		# A run alongside this one inserted it first, before any machine was rented here.
		frappe.db.rollback()
		return None
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			title=f"Could not start a {image['image_type']} image of Pilot {image['pilot_version']}"
		)
		return None

	return name


def start_base_images(force: bool = False) -> list[str]:
	"""Start a Base image of the newest Pilot release on the head commit of each Frappe branch.
	A new Pilot release and a new Frappe commit both lead here, and the image is built once.
	`force` runs it while release tracking is off."""
	if not (force or is_tracking_releases()):
		return []

	pilot_version = get_latest_pilot_release()
	started = []
	for frappe_branch in get_frappe_branches():
		image = {
			"pilot_version": pilot_version,
			"frappe_branch": frappe_branch,
			"frappe_commit": get_frappe_head_commit(frappe_branch),
			"image_type": "Base",
		}
		if name := start_image(image):
			started.append(name)

	return started


def start_site_images() -> list[str]:
	"""Start a Site image on the newest Completed Base image of each Frappe branch, when it has
	none. Returns the images started."""
	if not is_tracking_releases():
		return []

	started = []
	for frappe_branch in get_frappe_branches():
		base_image = get_newest_completed_image("Base", frappe_branch)
		if not base_image:
			continue

		image = {
			"pilot_version": base_image.pilot_version,
			"frappe_branch": frappe_branch,
			"frappe_commit": base_image.frappe_commit,
			"image_type": "Site",
		}
		if name := start_image(image):
			started.append(name)

	return started


def skip_app_release(release: AppRelease, frappe_version: str) -> bool:
	"""Whether the release gets no Apps image: a requirement has no stable release, or Cargo
	Settings require prebuilt assets and an app it installs has none."""
	if not frappe.db.get_single_value("Cargo Settings", "require_prebuilt_assets"):
		return False

	try:
		install_order = get_install_order(release, frappe_version)
	except frappe.ValidationError:
		# A requirement has no stable release yet, so the app cannot be installed.
		return True

	return not all(do_assets_exist(app) for app in install_order)


def start_apps_images() -> list[str]:
	"""Start an Apps image for each signup app whose newest stable release has none, on the
	newest Completed Site image of each Frappe branch. A newer Site image alone starts nothing:
	the app's next release builds on it. Returns the images started."""
	if not is_tracking_releases():
		return []

	started = []
	for frappe_branch in get_frappe_branches():
		if site_image := get_newest_completed_image("Site", frappe_branch):
			started += start_apps_images_on(site_image)

	return started


def start_apps_images_on(site_image: dict | PilotImage, force: bool = False) -> list[str]:
	"""Start an Apps image of each signup app's newest stable release on `site_image`. Unless
	`force` is set, a release that has an image on any Site image is skipped."""
	started = []
	for signup_app in get_signup_apps():
		release = get_latest_stable_release(signup_app, site_image.frappe_version)
		if not release:
			continue

		if not force and frappe.db.exists(
			"Pilot Image",
			{
				"image_type": "Apps",
				"frappe_branch": site_image.frappe_branch,
				"signup_app": signup_app,
				"app_commit": release.commit,
			},
		):
			continue

		if skip_app_release(release, site_image.frappe_version):
			continue

		image = {
			"pilot_version": site_image.pilot_version,
			"frappe_branch": site_image.frappe_branch,
			"frappe_commit": site_image.frappe_commit,
			"image_type": "Apps",
			"signup_app": signup_app,
			"app_commit": release.commit,
		}
		if name := start_image(image):
			started.append(name)

	return started


def get_images_to_retry(frappe_branch: str) -> list[str]:
	"""The Base and Site images of the newest build on `frappe_branch`, and the newest Apps
	image of each signup app on it."""
	images = []
	builds = get_builds(frappe_branch)
	if builds:
		images += frappe.get_all(
			"Pilot Image",
			filters={
				"frappe_branch": frappe_branch,
				"pilot_version": builds[0].pilot_version,
				"frappe_commit": builds[0].frappe_commit,
				"image_type": ("in", ("Base", "Site")),
			},
			pluck="name",
		)

	for signup_app in get_signup_apps():
		newest = frappe.db.get_value(
			"Pilot Image",
			{"frappe_branch": frappe_branch, "image_type": "Apps", "signup_app": signup_app},
			order_by="creation desc",
		)
		if newest:
			images.append(newest)

	return images


def retry_failed_images() -> list[str]:
	"""Restart the newest failed images on each Frappe branch, up to Cargo Settings'
	`max_auto_retry_count` times each. Returns the restarted images."""
	max_retries = frappe.db.get_single_value("Cargo Settings", "max_auto_retry_count")
	restarted = []
	for frappe_branch in get_frappe_branches():
		images = get_images_to_retry(frappe_branch)
		if not images:
			continue

		failed_images = frappe.get_all(
			"Pilot Image",
			filters={
				"name": ("in", images),
				"status": "Failed",
				"manually_failed": False,
				"auto_retry_count": ("<", max_retries),
			},
			pluck="name",
		)
		for name in failed_images:
			try:
				frappe.get_doc("Pilot Image", name).restart_build(automatic=True)
				# One image's restart must not roll back another's, whose Atlas images are already gone.
				frappe.db.commit()  # nosemgrep
			except Exception:
				frappe.db.rollback()
				frappe.log_error(title=f"Could not restart Pilot Image {name}")
				continue

			restarted.append(name)

	return restarted


def retire_older_images() -> list[str]:
	"""Retire images that newer ones replace on their Frappe branch. Returns the retired images."""
	retired = []
	for frappe_branch in get_frappe_branches():
		for name in get_older_builds_images(frappe_branch) + get_older_apps_images(frappe_branch):
			try:
				frappe.get_doc("Pilot Image", name).retire()
				frappe.db.commit()  # nosemgrep
			except Exception:
				frappe.db.rollback()
				frappe.log_error(title=f"Could not retire Pilot Image {name}")
				continue

			retired.append(name)

	return retired


def get_older_builds_images(frappe_branch: str) -> list[str]:
	"""The finished Base and Site images of builds older than the three newest completed ones."""
	kept = get_builds(frappe_branch, completed_only=True)[:3]
	if len(kept) < 3:
		return []

	# A build first seen after these has not completed and is still being retried, so only
	# builds first seen before the oldest kept one go, with every image they have.
	oldest_kept = min(build.first_created for build in kept)
	older_images = []
	for build in get_builds(frappe_branch):
		if build.first_created >= oldest_kept:
			continue

		build_filters = {
			"frappe_branch": frappe_branch,
			"pilot_version": build.pilot_version,
			"frappe_commit": build.frappe_commit,
		}
		image_types = ["Base", "Site"]
		if frappe.db.exists(
			"Pilot Image",
			{**build_filters, "image_type": "Apps", "status": ("not in", ("Completed", "Retired"))},
		):
			image_types = ["Base"]

		older_images += frappe.get_all(
			"Pilot Image",
			filters={
				**build_filters,
				"image_type": ("in", image_types),
				"status": ("in", ("Completed", "Failed")),
			},
			pluck="name",
		)

	return older_images


def get_older_apps_images(frappe_branch: str) -> list[str]:
	"""The finished Apps images of each signup app older than its three newest completed ones.
	Every Apps image is a full disk, so retiring the Site image it booted on leaves it working."""
	older_images = []
	for signup_app in get_signup_apps():
		filters = {"frappe_branch": frappe_branch, "image_type": "Apps", "signup_app": signup_app}
		kept = frappe.get_all(
			"Pilot Image",
			filters={**filters, "status": "Completed"},
			pluck="creation",
			order_by="creation desc",
			limit=3,
		)
		if len(kept) < 3:
			continue

		older_images += frappe.get_all(
			"Pilot Image",
			filters={**filters, "status": ("in", ("Completed", "Failed")), "creation": ("<", kept[-1])},
			pluck="name",
		)

	return older_images


def on_doctype_update():
	# Earlier keys left out the Frappe commit and the signup app, and would refuse a new build.
	for old_key in ("unique_pilot_image_variant", "unique_pilot_image_build"):
		if frappe.db.has_index("tabPilot Image", old_key):
			frappe.db.sql_ddl(f"ALTER TABLE `tabPilot Image` DROP INDEX `{old_key}`")

	frappe.db.add_unique(
		"Pilot Image",
		["pilot_version", "image_type", "frappe_branch", "frappe_commit", "signup_app", "app_commit"],
		constraint_name="unique_pilot_image",
	)
