# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt
import json
import typing

import frappe
from frappe import _

from cargo.atlas_client import AtlasClient, AtlasNotFound, base_image_id
from cargo.cargo.doctype.machine.machine import DEAD_MACHINE_STATES
from cargo.cargo.doctype.machine.machine import Machine as MachineDoc
from cargo.image_builder.doctype.pilot_image.apps import (
	SIGNUP_APPS,
	AppRelease,
	get_compatible_app_commit,
)
from cargo.image_builder.doctype.pilot_image.builder import (
	BUILD_SPEC,
	PING_TIMEOUT,
	PROVISION_TIMEOUT,
	SSH_READY_TIMEOUT,
	Builder,
)
from cargo.image_builder.doctype.pilot_image.releases import frappe_release
from cargo.ssh import OutputLog, script
from cargo.workflow_engine.doctype.press_workflow.decorators import flow, task
from cargo.workflow_engine.doctype.press_workflow.workflow_builder import WorkflowBuilder

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings
	from cargo.image_builder.doctype.pilot_image_snapshot.pilot_image_snapshot import PilotImageSnapshot

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

		build_log: DF.Code | None
		error: DF.LongText | None
		frappe_branch: DF.Literal["version-16", "develop"]
		frappe_version: DF.Data | None
		image_type: DF.Literal["Base", "Site", "Apps"]
		machine: DF.Link | None
		pilot_version: DF.Data
		status: DF.Literal["Provisioning", "Building", "Snapshotting", "Completed", "Failed"]
	# end: auto-generated types

	"""This doctype is only responsible to run the provisioning script based on the
	supplied environment and plan and trigger snapshot creations."""

	def after_insert(self) -> None:
		"""Request a build machine from atlas."""
		self.request_build_machine()

	def request_build_machine(self) -> None:
		"""Rent a machine to build on. `sync_machines` starts the build once it is running."""
		self.machine = MachineDoc.request(owner=self, spec=BUILD_SPEC, base_image=base_image_id()).name
		self.status = "Provisioning"
		self.save()

	@property
	def build_machine(self) -> MachineDoc | None:
		if not self.machine:
			return None

		return frappe.get_doc("Machine", self.machine)

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
		"""Stop the build process"""
		if self.status not in ("Provisioning", "Building", "Snapshotting"):
			frappe.throw(_("Only a build in progress can be stopped."))

		workflow = frappe.db.get_value(
			"Press Workflow",
			{
				"linked_doctype": self.doctype,
				"linked_docname": self.name,
				"status": ("in", ("Queued", "Running")),
			},
		)
		if workflow:
			frappe.get_doc("Press Workflow", workflow).force_fail()
			return

		self.status = "Failed"
		self.error = _("Build stopped by {0}.").format(frappe.session.user)
		self.save()
		self.release_build_machine()

	@frappe.whitelist()
	def restart_build(self) -> None:
		"""Build this image again from the start, on a new machine."""
		if self.status != "Failed":
			frappe.throw(_("Cannot restart a build that is not in 'Failed' status."))

		self.delete_atlas_images()
		self.release_build_machine()

		for snapshot in frappe.get_all(
			"Pilot Image Snapshot", filters={"pilot_image": self.name}, pluck="name"
		):
			frappe.delete_doc("Pilot Image Snapshot", snapshot)

		self.error = None
		self.build_log = None
		self.frappe_version = None
		self.request_build_machine()

	@flow
	def _create_image(self):
		# Snapshots come first: their pinned releases are what the provision script fetches.
		self.create_snapshots()
		self.run_provision_script()

		snapshots = frappe.get_all(
			"Pilot Image Snapshot", filters={"pilot_image": self.name}, order_by="creation asc", pluck="name"
		)

		for snapshot in snapshots:
			self.mark_snapshotting(snapshot)
			self.start_snapshotting(snapshot)
			self.wait_for_snapshot(snapshot)
			self.finish_snapshotting(snapshot)

	@task(queue="short")
	def mark_snapshotting(self, snapshot_name: str) -> None:
		"""Move the snapshot about to be taken, and the image with its first one, to Snapshotting."""
		if self.status != "Snapshotting":
			self.status = "Snapshotting"
			self.save()

		frappe.db.set_value("Pilot Image Snapshot", snapshot_name, "status", "Snapshotting")

	@task(queue="long", timeout=3600)
	def start_snapshotting(self, snapshot_name: str) -> None:
		"""Put the site in the snapshot's state, then ask Atlas to photograph the machine."""
		snapshot: PilotImageSnapshot = frappe.get_doc("Pilot Image Snapshot", snapshot_name)
		machine = self.build_machine

		snapshot.run_app_prerequisite(machine, self)
		snapshot.take(machine, self)

	@task(queue="short")
	def wait_for_snapshot(self, snapshot_name: str) -> None:
		"""Wait for Atlas to make the image. A deferred task is run again about once a minute."""
		snapshot: PilotImageSnapshot = frappe.get_doc("Pilot Image Snapshot", snapshot_name)
		if snapshot.complete_if_available():
			return

		self.defer_current_task(_("Atlas is still making image {0}.").format(snapshot.snapshot_id))

	@task(queue="long", timeout=3600)
	def finish_snapshotting(self, snapshot_name: str) -> None:
		"""Undo the snapshot's site state, so the next snapshot starts from a bare site."""
		snapshot: PilotImageSnapshot = frappe.get_doc("Pilot Image Snapshot", snapshot_name)
		snapshot.run_app_post_requisite(self.build_machine, self)

	@task(queue="short")
	def create_snapshots(self) -> None:
		"""Create snapshots for the image based on the image type. Snapshot pre-requisite will
		prepare the apps to be installed before snapshot."""
		if frappe.db.exists("Pilot Image Snapshot", {"pilot_image": self.name}):
			return

		self.frappe_version = frappe_release(self.frappe_branch)
		self.save()

		if self.image_type == "Base":
			self.insert_snapshot(None, [])
			return

		releases: dict[str, AppRelease] = {}
		for signup_app in SIGNUP_APPS:
			required_apps = signup_app if isinstance(signup_app, tuple) else (signup_app,)

			for app in required_apps:
				if app not in releases:
					releases[app] = get_compatible_app_commit(app, self.frappe_version)

				missing = set(releases[app].requires) - set(required_apps)
				if missing:
					frappe.throw(
						_("{0} requires {1}, which is not listed with it.").format(app, ", ".join(missing))
					)

			if self.image_type == "Apps":
				# Since singup app is the last app in the required apps.
				self.insert_snapshot(required_apps[-1], [releases[app] for app in required_apps])

		if self.image_type == "Site":
			self.insert_snapshot(None, list(releases.values()))

	def insert_snapshot(self, signup_app: str | None, releases: list[AppRelease]) -> None:
		frappe.get_doc(
			{
				"doctype": "Pilot Image Snapshot",
				"pilot_image": self.name,
				"status": "Pending",
				"signup_app": signup_app,
				"required_apps": [
					{
						"app": release.name,
						"version": release.version,
						"commit": release.commit,
						"repo": release.repo,
					}
					for release in releases
				],
			}
		).insert()

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

	def get_required_apps(self) -> list[dict]:
		"""Every app this image's snapshots pin, once each, in install order. This is already
		calculated while creating the snapshot therefore reusing here."""
		snapshots = frappe.get_all("Pilot Image Snapshot", filters={"pilot_image": self.name}, pluck="name")
		if not snapshots:
			frappe.throw(_("Pilot Image {0} has no snapshots to fetch apps for.").format(self.name))

		rows = frappe.get_all(
			"Pilot Image Snapshot App",
			filters={
				"parenttype": "Pilot Image Snapshot",
				"parentfield": "required_apps",
				"parent": ("in", snapshots),
			},
			fields=["app", "repo", "commit"],
			order_by="parent asc, idx asc",
		)
		# Keyed by app, so an app two snapshots share appears once, where it first appears.
		apps = {row.app: row for row in rows}

		return list(apps.values())

	def get_provision_environment(self) -> dict[str, str]:
		"""What provision.sh reads. The bench, site and admin domain are the same for every
		image, so the script names those itself."""
		settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")
		proxy_subnet = f"fdaa:{int(settings.region_id or 0):x}::/64"
		domain_provider = (
			script(*DOMAIN_PROVIDER)
			.replace('"__WILDCARD_DOMAIN__"', json.dumps(f"*.{settings.wildcard_domain}"))
			.replace('"__PROXY_SUBNET__"', json.dumps(proxy_subnet))
		)

		return {
			"VERSION": self.pilot_version,
			"FRAPPE_BRANCH": self.frappe_branch,
			"WILDCARD_DOMAIN": settings.wildcard_domain or "",
			"PROXY_SUBNET": proxy_subnet,
			"DOMAIN_PROVIDER": domain_provider,
			"IMAGE_TYPE": self.image_type,
			"REQUIRED_APPS": ""
			if self.image_type == "Base"
			else "\n".join(f"{app.repo} {app.commit}" for app in self.get_required_apps()),
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
		traceback = ((failed and failed.traceback) or workflow.workflow_traceback or "").strip()

		# Recorded before the machine goes, so a failed release cannot hide why the build failed.
		self.status = "Failed"
		self.error = f"{stage}\n{traceback}"
		self.save()

		# Single failed snapshot will fail all other snapshots and the image itself.
		frappe.db.set_value(
			"Pilot Image Snapshot",
			{"pilot_image": self.name, "status": "Snapshotting"},
			{"status": "Failed", "error": traceback},
		)
		frappe.db.set_value(
			"Pilot Image Snapshot",
			{"pilot_image": self.name, "status": "Pending"},
			{
				"status": "Failed",
				"error": _("Not taken: the build failed at {0}. See Pilot Image {1}.").format(
					stage, self.name
				),
			},
		)
		frappe.db.set_value(
			"Pilot Image Snapshot",
			{"pilot_image": self.name, "status": "Available"},
			{
				"status": "Failed",
				"error": _(
					"Its Atlas image is deleted: the build failed at {0}. See Pilot Image {1}."
				).format(stage, self.name),
			},
		)

		self.release_build_machine()
		self.delete_atlas_images()

	def on_workflow_success(self, workflow) -> None:
		"""The workflow engine has finished this image. The machine is still running, but the
		image is built and the snapshots are taken."""
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

	def delete_atlas_images(self) -> None:
		"""Delete the Atlas images this build's snapshots took, so a failed build cannot be
		booted. Tries every image, then throws if Atlas refused any."""
		snapshot_ids = frappe.get_all(
			"Pilot Image Snapshot",
			filters={"pilot_image": self.name, "snapshot_id": ("is", "set")},
			pluck="snapshot_id",
		)
		client = AtlasClient.from_settings()
		refused = []
		for snapshot_id in snapshot_ids:
			try:
				client.delete_snapshot(snapshot_id)
			except AtlasNotFound:
				# Deleted by an earlier attempt.
				continue
			except Exception:
				frappe.log_error(
					title=f"Could not delete snapshot {snapshot_id} for failed image {self.name}",
					message=frappe.get_traceback(with_context=True),
				)
				refused.append(snapshot_id)

		if refused:
			frappe.throw(_("Atlas did not delete images {0}. See the Error Log.").format(", ".join(refused)))
