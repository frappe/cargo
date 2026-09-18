# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

import json
from itertools import product

import frappe
from frappe.utils import now_datetime

from cargo.atlas_client import (
	DEAD_STATES,
	PILOT_IMAGE_OS_TAGS,
	RUNNING_STATE,
	AtlasClient,
	AtlasNotFound,
)
from cargo.image_builder.doctype.pilot_image.builder import (
	PING_TIMEOUT,
	PROVISION_TIMEOUT,
	SSH_READY_TIMEOUT,
	Builder,
)
from cargo.image_builder.doctype.pilot_image.releases import latest_pilot_release
from cargo.ssh import OutputLog, create_keypair, script
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
		ssh_private_key: DF.Password | None
		ssh_public_key: DF.SmallText | None
		status: DF.Literal["Draft", "Provisioning", "Building", "Available", "Snapshotting", "Failed"]
		temporary_vm_id: DF.Data | None
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
			frappe.throw(
				frappe._("{0} on {1} already exists.").format(self.pilot_version, self.frappe_version)
			)

	@property
	def atlas_name(self) -> str:
		"""What the build machine and its snapshot are called at Atlas."""
		return f"{self.name}-{self.frappe_version}"

	@property
	def builder(self) -> Builder:
		return Builder(self.atlas_name)

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
			frappe.throw(frappe._("This image is already building."))

		# Checked before a machine is rented: without it the image reaches nothing.
		if not self.wildcard_domain:
			frappe.throw(frappe._("Set the wildcard domain in Cargo Settings before building."))

		if not self.region_id:
			frappe.throw(frappe._("Set the region ID in Cargo Settings before building."))

		self.build_log = None
		self.ssh_public_key, self.ssh_private_key = create_keypair(self.atlas_name)
		self.temporary_vm_id = self.builder.provision_build_machine(public_key=self.ssh_public_key)
		self.mark("Provisioning")

	def sync_build_vm(self) -> None:
		"""Move the image on once its machine is up. Scheduled, one machine at a time."""
		client = AtlasClient.from_settings()
		try:
			machine = client.get_vm(self.temporary_vm_id)
		except AtlasNotFound:
			self.mark("Failed", error="Atlas no longer has this build machine")
			return

		state = machine.get("current_state")
		if state in DEAD_STATES:
			self.mark("Failed", error=f"Atlas reported {state}")
			return

		if state != RUNNING_STATE:
			return

		address = machine.get("network", {}).get("mesh_ipv6")
		if not address:
			self.mark("Failed", error="Atlas reported no mesh address for this build machine")
			return

		# One transaction, so `retry_workflows` can find a build whose job never started.
		self.mark("Building")
		self.run_build.run_as_workflow(address=address, vm_id=self.temporary_vm_id)

	@flow
	def run_build(self, address: str, vm_id: str) -> None:
		"""Bake a machine and photograph it"""
		self.run_provision_script(address)
		self.take_snapshot(vm_id)

	@task(queue="long", timeout=BUILD_TIMEOUT)
	def run_provision_script(self, address: str) -> None:
		"""Install onto the build machine"""
		private_key = self.get_password("ssh_private_key")
		self.builder.wait_until_reachable(address, private_key)

		with OutputLog(self, "build_log") as log:
			self.builder.run_provision_script_on_build_machine(
				address,
				private_key,
				self.provision_environment,
				on_output=log.write,
			)

		# Here, not in the snapshot task: that one runs without a key.
		self.builder.flush_build_machine(address, private_key)

		# Atlas serves the authorized key from instance metadata on each attempt, so the
		# disk holds no key to take off it. Cargo drops its own half here.
		self.drop_ssh_keys()
		self.mark("Snapshotting")

	@frappe.whitelist()
	def stop_build(self) -> None:
		"""Give up on a build that is not moving, and destroy the machine it rented."""
		if self.status not in BUILDING_STATUSES:
			frappe.throw(frappe._("This image is not building."))

		if not self.temporary_vm_id or self.builder.destroy_build_machine(self.temporary_vm_id):
			self.temporary_vm_id = None
			self.drop_ssh_keys()

		self.mark("Failed", error=f"Build stopped by {frappe.session.user}.")

	@task(queue="long", timeout=SNAPSHOT_TIMEOUT)
	def take_snapshot(self, vm_id: str) -> None:
		"""Photograph the machine, then destroy it either way"""
		builder = self.builder
		try:
			snapshot = builder.snapshot_build_machine(vm_id, self.image_tags)
		finally:
			builder.destroy_build_machine(vm_id)

		self.snapshot_id = snapshot
		self.built_at = now_datetime()
		self.temporary_vm_id = None
		self.save(ignore_permissions=True)

	def on_workflow_success(self, workflow) -> None:
		self.mark("Available")

	def on_workflow_failure(self, workflow) -> None:
		"""Record which task failed, and make sure its machine is not left running."""
		failed = next((row for row in workflow.steps if row.status == "Failure"), None)
		stage = failed.step_title if failed else "Build"
		reason = frappe.db.get_value("Press Workflow Task", failed.task, "traceback") if failed else None
		error = (reason or workflow.workflow_traceback or "").strip()

		if not self.temporary_vm_id or self.builder.destroy_build_machine(self.temporary_vm_id):
			self.drop_ssh_keys()

		self.mark("Failed", error=f"{stage}\n{error}")

	def drop_ssh_keys(self) -> None:
		"""Cargo's half of a keypair whose machine is gone."""
		self.ssh_public_key = None
		self.ssh_private_key = None

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


def sync_build_machines() -> None:
	"""Walk every image still waiting on Atlas. Scheduled in `hooks.py`."""
	waiting = frappe.get_all(
		"Pilot Image",
		filters={"status": "Provisioning", "temporary_vm_id": ["is", "set"]},
		pluck="name",
	)
	for name in waiting:
		image: PilotImage = frappe.get_doc("Pilot Image", name)
		image.sync_build_vm()


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
		image: PilotImage = frappe.get_doc("Pilot Image", name)
		image.retire()


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
