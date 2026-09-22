# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

import json
from itertools import product

import frappe
from frappe import _
from frappe.utils import now_datetime

from cargo.atlas_client import PILOT_IMAGE_OS_TAGS, AtlasClient, AtlasNotFound, base_image_id
from cargo.cargo.doctype.machine.machine import DEAD_MACHINE_STATES
from cargo.cargo.doctype.machine.machine import Machine as MachineDoc
from cargo.image_builder.doctype.pilot_image.builder import (
	BUILD_SPEC,
	PING_TIMEOUT,
	PROVISION_TIMEOUT,
	SSH_READY_TIMEOUT,
	Builder,
)
from cargo.image_builder.doctype.pilot_image.releases import latest_pilot_release
from cargo.ssh import OutputLog, script
from cargo.workflow_engine.doctype.press_workflow.decorators import flow, task
from cargo.workflow_engine.doctype.press_workflow.workflow_builder import WorkflowBuilder

# The task holds the wait for the machine as well as the script it then runs, so a slow
# boot cannot eat into the time the script is allowed.
BUILD_TIMEOUT = PING_TIMEOUT + SSH_READY_TIMEOUT + PROVISION_TIMEOUT
SNAPSHOT_TIMEOUT = 1800
FRAPPE_VERSIONS = ("version-16", "develop")
SITE_VARIANTS = (1, 0)
TRACKED_PILOT_VERSIONS = 3
BUILDING_STATUSES = ("Provisioning", "Building", "Snapshotting")
DOMAIN_PROVIDER = ("image_builder", "conf", "pilot", "domain_provider.py")


class PilotImage(WorkflowBuilder):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		build_log: DF.Code | None
		built_at: DF.Datetime | None
		error: DF.LongText | None
		frappe_version: DF.Literal["version-16", "develop"]
		has_site: DF.Check
		pilot_version: DF.Data
		snapshot_id: DF.Data | None
		machine: DF.Link | None
		status: DF.Literal["Draft", "Provisioning", "Building", "Available", "Snapshotting", "Failed"]
	# end: auto-generated types

	"""One Pilot release baked against one Frappe version, and the snapshot it produced."""

	def validate(self) -> None:
		if frappe.db.exists(
			"Pilot Image",
			{
				"pilot_version": self.pilot_version,
				"frappe_version": self.frappe_version,
				"has_site": self.has_site,
				"name": ("!=", self.name),
			},
		):
			frappe.throw(_("{0} on {1} already exists.").format(self.pilot_version, self.frappe_version))

	@property
	def atlas_name(self) -> str:
		"""What this image's snapshot is called at Atlas."""
		return f"{self.name}-{self.frappe_version}"

	@property
	def builder(self) -> Builder:
		return Builder()

	@property
	def image_tags(self) -> dict[str, str]:
		"""What the snapshot is labelled with at Atlas, so a search finds it by version."""
		return {
			"purpose": "pilot",
			"pilot_version": self.pilot_version,
			"frappe_version": self.frappe_version,
			"has_site": str(int(self.has_site)),
			**PILOT_IMAGE_OS_TAGS,
		}

	@property
	def provision_environment(self) -> dict[str, str]:
		"""What the provision script reads. The bench, site and admin domain are the same
		for every image, so the script names those itself."""
		return {
			"VERSION": self.pilot_version,
			"FRAPPE_VERSION": self.frappe_version,
			"WILDCARD_DOMAIN": self.wildcard_domain,
			"PROXY_SUBNET": self.proxy_subnet,
			"DOMAIN_PROVIDER": self.domain_provider,
			"HAS_SITE": str(int(self.has_site)),
		}

	@property
	def wildcard_domain(self) -> str:
		"""The zone every VM hostname sits under. The aliases are built from it."""
		return frappe.db.get_single_value("Cargo Settings", "wildcard_domain") or ""

	@property
	def region_id(self) -> int:
		return int(frappe.db.get_single_value("Cargo Settings", "region_id") or 0)

	@property
	def proxy_subnet(self) -> str:
		"""The tenant-0 subnet holding every edge proxy. A mesh address is
		fdaa:<region>:<tenant>:<VM>, so one tenant of one region is a /64."""
		return f"fdaa:{self.region_id:x}::/64"

	@property
	def domain_provider(self) -> str:
		"""The provider the image installs, with this region's values baked in."""
		source = script(*DOMAIN_PROVIDER)
		return source.replace('"__WILDCARD_DOMAIN__"', json.dumps(f"*.{self.wildcard_domain}")).replace(
			'"__PROXY_SUBNET__"', json.dumps(self.proxy_subnet)
		)

	@frappe.whitelist()
	def build(self) -> None:
		"""Ask Atlas for a machine to bake. The scheduler takes it from here."""
		if self.status in BUILDING_STATUSES:
			frappe.throw(_("This image is already building."))

		# Checked before a machine is rented: without it the image reaches nothing.
		if not self.wildcard_domain:
			frappe.throw(_("Set the wildcard domain in Cargo Settings before building."))

		if not self.region_id:
			frappe.throw(_("Set the region ID in Cargo Settings before building."))

		self.build_log = None
		self.machine = MachineDoc.request(self, BUILD_SPEC, base_image=base_image_id()).name
		self.mark("Provisioning")

	def sync_machines(self) -> None:
		"""What this image's build machine settling means for it. Its state is already
		recorded; `sync_pending_machines` calls this once it changes."""
		if self.status != "Provisioning":
			return

		machine: MachineDoc = frappe.get_doc("Machine", self.machine)
		if machine.status in DEAD_MACHINE_STATES:
			self.mark("Failed", error=machine.error or f"Build machine is {machine.status}")
			return

		if machine.status != "Running":
			return

		# One transaction, so `retry_workflows` can find a build whose job never started.
		self.mark("Building")
		self.run_build.run_as_workflow(address=machine.address)

	@flow
	def run_build(self, address: str) -> None:
		"""Bake a machine and photograph it"""
		self.run_provision_script(address)
		self.take_snapshot()

	@task(queue="long", timeout=BUILD_TIMEOUT)
	def run_provision_script(self, address: str) -> None:
		"""Install onto the build machine"""
		private_key = self.build_machine.get_password("ssh_private_key")
		self.builder.wait_until_reachable(address, private_key)

		with OutputLog(self, "build_log") as log:
			self.builder.run_provision_script_on_build_machine(
				address,
				private_key,
				self.provision_environment,
				on_output=log.write,
			)

		# Here, not in the snapshot task: that one works from the machine record alone.
		self.builder.flush_build_machine(address, private_key)

		self.mark("Snapshotting")

	@frappe.whitelist()
	def stop_build(self) -> None:
		"""Give up on a build that is not moving, and release the machine it rented."""
		if self.status not in BUILDING_STATUSES:
			frappe.throw(_("This image is not building."))

		workflow = self.running_workflow
		if workflow:
			frappe.get_doc("Press Workflow", workflow).force_fail()
			return

		if not self.release_build_machine():
			frappe.throw(_("Atlas did not destroy the build machine."))

		self.mark("Failed", error=f"Build stopped by {frappe.session.user}.")

	@property
	def running_workflow(self) -> str | None:
		"""This image's build workflow while it is still going, or None."""
		return frappe.db.get_value(
			"Press Workflow",
			{
				"linked_doctype": self.doctype,
				"linked_docname": self.name,
				"status": ("in", ("Queued", "Running")),
			},
		)

	@task(queue="long", timeout=SNAPSHOT_TIMEOUT)
	def take_snapshot(self) -> None:
		"""Photograph the machine, then destroy it either way"""
		machine = self.build_machine
		try:
			snapshot = machine.snapshot(self.atlas_name, self.image_tags)
		finally:
			machine.terminate()

		self.snapshot_id = snapshot
		self.built_at = now_datetime()
		self.save(ignore_permissions=True)

	def on_workflow_success(self, workflow) -> None:
		self.mark("Available")

	def on_workflow_failure(self, workflow) -> None:
		"""Record which task failed, and make sure its machine is not left running."""
		failed = next((row for row in workflow.steps if row.status == "Failure"), None)
		stage = failed.step_title if failed else "Build"
		reason = frappe.db.get_value("Press Workflow Task", failed.task, "traceback") if failed else None
		error = (reason or workflow.workflow_traceback or "").strip()

		self.release_build_machine()
		self.mark("Failed", error=f"{stage}\n{error}")

	@property
	def build_machine(self) -> MachineDoc:
		return frappe.get_doc("Machine", self.machine)

	def release_build_machine(self) -> bool:
		"""Let this build's machine go. False means Atlas still has it running."""
		if not self.machine:
			return True

		machine = self.build_machine
		if machine.status == "Terminated":
			return True

		return machine.terminate()

	def mark(self, status: str, error: str | None = None) -> None:
		self.status = status
		self.error = error
		self.save(ignore_permissions=True)

	def retire(self) -> bool:
		"""Drop the Atlas snapshot, then this record. False leaves both for the next run.

		Atlas never refuses an image a machine still uses. It archives the image and
		reclaims it when the last machine goes, so no usage check is needed here."""
		if self.snapshot_id:
			try:
				AtlasClient.from_settings().delete_snapshot(self.snapshot_id)
			except AtlasNotFound:
				pass
			except Exception:
				frappe.log_error(title=f"Could not delete snapshot {self.snapshot_id}")
				return False

		# The record goes last, or a failed call above would leak the snapshot.
		self.delete(ignore_permissions=True)
		return True

	def on_trash(self) -> None:
		"""Kill machine in case image is deleted"""
		super().on_trash()
		if self.release_build_machine():
			frappe.db.delete("Machine", {"reference_doctype": self.doctype, "reference_name": self.name})
			return
		frappe.throw(_("Unable to terminate VM {0} before deleteion.").format(self.name))


def sync_pilot_releases() -> None:
	"""Build the newest Pilot release, then drop what fell out of the window.

	Off by default. Scheduled in `hooks.py`."""
	if not frappe.db.get_single_value("Cargo Settings", "track_pilot_releases"):
		return

	for name in ensure_release(latest_pilot_release()):
		enqueue_build(name)

	retire_old_releases()


def ensure_release(pilot_version: str) -> list[str]:
	"""One record per Frappe version and site variant. Returns those waiting for a machine.

	A build that never started leaves the image in Draft, so the next run picks it up.
	A Failed image is left alone, because retrying it hourly would rent a machine hourly."""
	waiting = []
	for frappe_version, has_site in product(FRAPPE_VERSIONS, SITE_VARIANTS):
		identity = {
			"pilot_version": pilot_version,
			"frappe_version": frappe_version,
			"has_site": has_site,
		}
		name = frappe.db.exists("Pilot Image", identity)
		if not name:
			name = frappe.get_doc({"doctype": "Pilot Image", **identity}).insert().name

		if frappe.db.get_value("Pilot Image", name, "status") == "Draft":
			waiting.append(name)

	return waiting


def enqueue_build(name: str) -> None:
	"""One job per image, so each rented machine lands in a transaction of its own."""
	frappe.enqueue(
		"cargo.image_builder.doctype.pilot_image.pilot_image.build_image",
		queue="short",
		job_id=f"cargo||pilot_image||build||{name}",
		deduplicate=True,
		enqueue_after_commit=True,
		name=name,
	)


def build_image(name: str) -> None:
	"""Rent a machine for one image. Its own job, so one failure cannot undo another."""
	image: PilotImage = frappe.get_doc("Pilot Image", name)
	if image.status != "Draft":
		return

	image.build()


def retire_old_releases() -> None:
	"""Keep the newest few Pilot versions, by the order Cargo first saw each one."""
	keep = tracked_pilot_versions()
	if not keep:
		return

	stale = frappe.get_all(
		"Pilot Image",
		filters={"pilot_version": ("not in", keep), "status": ("not in", BUILDING_STATUSES)},
		pluck="name",
	)
	for name in stale:
		try:
			image: PilotImage = frappe.get_doc("Pilot Image", name)
			image.retire()

			if not frappe.flags.in_test:
				frappe.db.commit()  # nosemgrep
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Could not retire image {name}")


def on_doctype_update() -> None:
	"""The three together are the identity, so the database holds them, not only `validate`."""
	frappe.db.add_unique(
		"Pilot Image",
		["pilot_version", "frappe_version", "has_site"],
		constraint_name="unique_pilot_image_variant",
	)


def tracked_pilot_versions() -> list[str]:
	"""The Pilot versions inside the window, newest first.

	A version is placed by the first image Cargo made for it, so building one again
	later does not move it."""
	first_seen: list[str] = []
	for version in frappe.get_all("Pilot Image", order_by="creation asc", pluck="pilot_version"):
		if version not in first_seen:
			first_seen.append(version)

	return first_seen[-TRACKED_PILOT_VERSIONS:][::-1]
