# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

import secrets
import string

import frappe
from frappe.utils import now_datetime

from cargo.atlas_client import DEAD_STATES, RUNNING_STATE, AtlasClient, AtlasNotFound
from cargo.image_builder.builder import Builder
from cargo.image_builder.releases import latest_pilot_release
from cargo.ssh import OutputLog, create_keypair
from cargo.workflow_engine.doctype.press_workflow.decorators import flow, task
from cargo.workflow_engine.doctype.press_workflow.workflow_builder import WorkflowBuilder

BUILD_TIMEOUT = 3600
SNAPSHOT_TIMEOUT = 1800
SITE_DOMAIN = "frappe.cloud"
# Reached only through the admin hostname alias, so it never has to resolve.
ADMIN_DOMAIN = "admin.local"
FRAPPE_VERSIONS = ("version-16", "develop")
TRACKED_PILOT_VERSIONS = 3
BUILDING_STATUSES = ("Provisioning", "Building", "Snapshotting")
NAME_LENGTH = 8
PASSWORD_LENGTH = 24
PASSWORD_GROUPS = (string.ascii_uppercase, string.ascii_lowercase, string.digits, "!@#%^*+-=_")


class Image(WorkflowBuilder):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		admin_password: DF.Password | None
		bench_name: DF.Data | None
		build_log: DF.Code | None
		built_at: DF.Datetime | None
		error: DF.LongText | None
		frappe_version: DF.Literal["version-16", "develop"]
		pilot_version: DF.Data
		site_name: DF.Data | None
		snapshot_id: DF.Data | None
		ssh_private_key: DF.Password | None
		ssh_public_key: DF.SmallText | None
		status: DF.Literal["Draft", "Provisioning", "Building", "Available", "Snapshotting", "Failed"]
		temporary_vm_id: DF.Data | None
	# end: auto-generated types

	"""One Pilot release baked against one Frappe version, and the snapshot it produced."""

	def validate(self) -> None:
		if frappe.db.exists(
			"Image",
			{
				"pilot_version": self.pilot_version,
				"frappe_version": self.frappe_version,
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
	def provision_environment(self) -> dict[str, str]:
		"""What the provision script reads. The wildcard domain is what the hostname
		aliases match, so a host reaches its site and admin panel under its VM hostname."""
		return {
			"VERSION": self.pilot_version,
			"FRAPPE_VERSION": self.frappe_version,
			"SITE": self.site_name,
			"BENCH": self.bench_name,
			"ADMIN_DOMAIN": ADMIN_DOMAIN,
			"WILDCARD_DOMAIN": self.wildcard_domain,
			"ADMIN_PASSWORD": self.get_password("admin_password"),
		}

	@property
	def wildcard_domain(self) -> str:
		"""The zone every VM hostname sits under. The aliases are built from it."""
		return frappe.db.get_single_value("Cargo Settings", "wildcard_domain") or ""

	def name_contents(self) -> None:
		"""Name the bench and site this image carries, kept across rebuilds."""
		self.bench_name = self.bench_name or f"bench-{frappe.generate_hash(length=NAME_LENGTH)}"

		if not self.site_name:
			region = frappe.db.get_single_value("Cargo Settings", "region")
			self.site_name = f"{frappe.generate_hash(length=NAME_LENGTH)}.{region}.{SITE_DOMAIN}"

	@frappe.whitelist()
	def build(self) -> None:
		"""Ask Atlas for a machine to bake. The scheduler takes it from here."""
		if self.status in BUILDING_STATUSES:
			frappe.throw(frappe._("This image is already building."))

		# Checked before a machine is rented: without it the image reaches nothing.
		if not self.wildcard_domain:
			frappe.throw(frappe._("Set the wildcard domain in Cargo Settings before building."))

		self.build_log = None
		self.name_contents()
		self.ssh_public_key, self.ssh_private_key = create_keypair(self.atlas_name)
		self.admin_password = generate_admin_password()
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
		with OutputLog(self, "build_log") as log:
			self.builder.run_provision_script_on_build_machine(
				address,
				self.get_password("ssh_private_key"),
				self.provision_environment,
				on_output=log.write,
			)

		# The machine loses the key, then Cargo loses its half. Both before the snapshot.
		self.builder.wipe_machine_identity(address, self.get_password("ssh_private_key"))
		self.drop_ssh_keys()
		self.mark("Snapshotting")

	@task(queue="long", timeout=SNAPSHOT_TIMEOUT)
	def take_snapshot(self, vm_id: str) -> None:
		"""Photograph the machine, then destroy it either way"""
		builder = self.builder
		try:
			snapshot = builder.snapshot_build_machine(vm_id)
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


def generate_admin_password() -> str:
	"""A password pilot will accept: upper, lower, digit and symbol."""
	characters = [secrets.choice(group) for group in PASSWORD_GROUPS]
	pool = "".join(PASSWORD_GROUPS)
	characters += [secrets.choice(pool) for _ in range(PASSWORD_LENGTH - len(PASSWORD_GROUPS))]
	secrets.SystemRandom().shuffle(characters)

	return "".join(characters)


def sync_build_machines() -> None:
	"""Walk every image still waiting on Atlas. Scheduled in `hooks.py`."""
	waiting = frappe.get_all(
		"Image",
		filters={"status": "Provisioning", "temporary_vm_id": ["is", "set"]},
		pluck="name",
	)
	for name in waiting:
		image: Image = frappe.get_doc("Image", name)
		image.sync_build_vm()


def sync_pilot_releases() -> None:
	"""Build the newest Pilot release, then drop what fell out of the window.

	Off by default. Scheduled in `hooks.py`."""
	if not frappe.db.get_single_value("Cargo Settings", "track_pilot_releases"):
		return

	build_release(latest_pilot_release())
	retire_old_releases()


def build_release(pilot_version: str) -> list[str]:
	"""Give this release one built image per Frappe version.

	A build that never started leaves the image in Draft, so the next run picks it up.
	A Failed image is left alone, because retrying it hourly would rent a machine hourly."""
	started = []
	for frappe_version in FRAPPE_VERSIONS:
		name = frappe.db.exists("Image", {"pilot_version": pilot_version, "frappe_version": frappe_version})
		image: Image = (
			frappe.get_doc("Image", name)
			if name
			else frappe.get_doc(
				{"doctype": "Image", "pilot_version": pilot_version, "frappe_version": frappe_version}
			).insert()
		)
		if image.status != "Draft":
			continue

		image.build()
		# The machine is rented now, so its id is committed before the next one is asked
		# for. A later failure would otherwise roll back the record that holds the id and
		# leave the machine running with nothing tracking it.
		frappe.db.commit()  # nosemgrep
		started.append(image.name)

	return started


def retire_old_releases() -> None:
	"""Keep the newest few Pilot versions, by the order Cargo first saw each one."""
	keep = tracked_pilot_versions()
	if not keep:
		return

	stale = frappe.get_all(
		"Image",
		filters={"pilot_version": ("not in", keep), "status": ("not in", BUILDING_STATUSES)},
		pluck="name",
	)
	for name in stale:
		image: Image = frappe.get_doc("Image", name)
		image.retire()


def on_doctype_update() -> None:
	"""The pair is the identity, so the database holds it, not only `validate`."""
	frappe.db.add_unique("Image", ["pilot_version", "frappe_version"], constraint_name="unique_image_pair")


def tracked_pilot_versions() -> list[str]:
	"""The Pilot versions inside the window, newest first.

	A version is placed by the first image Cargo made for it, so building one again
	later does not move it."""
	first_seen: list[str] = []
	for version in frappe.get_all("Image", order_by="creation asc", pluck="pilot_version"):
		if version not in first_seen:
			first_seen.append(version)

	return first_seen[-TRACKED_PILOT_VERSIONS:][::-1]
