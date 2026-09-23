# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt
import json
import typing
from itertools import product

import frappe
from frappe import _

from cargo.atlas_client import PILOT_IMAGE_OS_TAGS, base_image_id
from cargo.cargo.doctype.machine.machine import DEAD_MACHINE_STATES
from cargo.cargo.doctype.machine.machine import Machine as MachineDoc
from cargo.image_builder.doctype.pilot_image.apps import (
	SIGNUP_APPS,
	AppRelease,
	get_compatible_app_commit,
	required_apps,
	resolve_releases,
)
from cargo.image_builder.doctype.pilot_image.builder import (
	BUILD_SPEC,
	PING_TIMEOUT,
	PROVISION_TIMEOUT,
	SSH_READY_TIMEOUT,
	Builder,
)
from cargo.image_builder.doctype.pilot_image.releases import frappe_release, latest_pilot_release
from cargo.image_builder.doctype.pilot_image_snapshot.pilot_image_snapshot import PilotImageSnapshot
from cargo.ssh import OutputLog, script
from cargo.workflow_engine.doctype.press_workflow.decorators import flow, task
from cargo.workflow_engine.doctype.press_workflow.workflow_builder import WorkflowBuilder

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings

# The task holds the wait for the machine as well as the script it then runs, so a slow
# boot cannot eat into the time the script is allowed.
BUILD_TIMEOUT = PING_TIMEOUT + SSH_READY_TIMEOUT + PROVISION_TIMEOUT
SNAPSHOT_TIMEOUT = 1800
FRAPPE_VERSIONS = ("version-16", "develop")
# (has_site, has_apps): a site ships bare or with every app installed and turned off.
IMAGE_VARIANTS = ((1, 0), (1, 1), (0, 0))
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
		error: DF.LongText | None
		frappe_version: DF.Literal["version-16", "develop"]
		has_apps: DF.Check
		has_site: DF.Check
		machine: DF.Link | None
		pilot_version: DF.Data
		status: DF.Literal["Draft", "Provisioning", "Building", "Snapshotting", "Completed", "Failed"]
	# end: auto-generated types

	"""This doctype is only responsible to run the provisioning script based on the
	supplied environment and plan and trigger snapshot creations."""

	def after_insert(self) -> None:
		"""Request a build machine from atlas"""
		self.machine = MachineDoc.request(owner=self, spec=BUILD_SPEC, base_image=base_image_id()).name
		# Update status here as well. — We can get rid of "Draft Status"
		self.save()

	def sync_machines(self) -> None:
		"""Once the machine is live we can start the initial build process."""
		machine_status = frappe.db.get_value("Machine", self.machine, "status")
		if machine_status in DEAD_MACHINE_STATES:
			self.status = "Failed"
			self.error = f"Machine {self.machine} is dead. Status: {machine_status}"
			self.save()
			return

		if machine_status == "Running" and self.status == "Draft":
			self.status = "Building"
			self.save()
			self.create_image()
			return

		# Any thing other than Dead and Running is a transient state, wait for it.
		return

	def create_image(self) -> None:
		"""Build the pilot image on the machine."""
		if self.status == "Building":
			frappe.throw(_("Cannot build a pilot image that is not in 'Building' status."))

		self._create_image.run_as_workflow()

	@flow
	def _create_image(self):
		self.run_provision_script()
		self.create_snapshot_records()

	@task(queue="long", timeout=BUILD_TIMEOUT)
	def run_provision_script(self) -> None:
		"""Run the provision script on the machine."""
		machine: MachineDoc = frappe.get_doc("Machine", self.machine)
		private_key = machine.get_password("ssh_private_key")
		builder = Builder()
		builder.wait_until_reachable(machine.address, private_key)

		with OutputLog(self, "build_log") as log:
			builder.run_provision_script_on_build_machine(
				machine.address,
				private_key,
				self.provision_environment(),
				on_output=log.write,
			)

	@task
	def create_snapshot_records(self) -> None:
		"""Create snapshot records for the build image."""
		...

	def install_app_commands(self) -> list[str]:
		"""Get all the apps and their versions to be installed on the pilot image"""
		if not self.has_apps:
			return []

		commands = []
		# Reuse this later when implementing snapshot creations for this pilot image
		self.app_releases = []
		pilot_frappe_version = frappe_release(self.frappe_version)
		for app in SIGNUP_APPS:
			app_release = get_compatible_app_commit(app, pilot_frappe_version)
			commands.append(f"pilot get-app {app_release.repo} --branch {app_release.commit}")
			self.app_releases.append(app_release)

		return commands

	def provision_environment(self) -> dict[str, str]:
		"""What the provision script reads. The bench, site and admin domain are the same
		for every image, so the script names those itself."""
		settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")
		proxy_subnet = f"fdaa:{int(settings.region_id or 0):x}::/64"
		domain_provider = (
			script(*DOMAIN_PROVIDER)
			.replace('"__WILDCARD_DOMAIN__"', json.dumps(f"*.{settings.wildcard_domain}"))
			.replace('"__PROXY_SUBNET__"', json.dumps(proxy_subnet))
		)

		return {
			"VERSION": self.pilot_version,
			"FRAPPE_VERSION": self.frappe_version,
			"WILDCARD_DOMAIN": settings.wildcard_domain or "",
			"PROXY_SUBNET": proxy_subnet,
			"DOMAIN_PROVIDER": domain_provider,
			"HAS_SITE": str(int(self.has_site)),
			"INSTALL_APP_COMMANDS": "\n".join(self.install_app_commands()),
		}
