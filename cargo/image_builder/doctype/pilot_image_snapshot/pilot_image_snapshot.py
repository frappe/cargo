# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

import typing

import frappe
import semantic_version as semvar
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from cargo.atlas_client import PILOT_IMAGE_OS_TAGS
from cargo.cargo.doctype.machine.machine import Machine as MachineDoc
from cargo.image_builder.doctype.pilot_image.builder import Builder
from cargo.ssh import OutputLog, script

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings
	from cargo.image_builder.doctype.pilot_image.pilot_image import PilotImage


class PilotImageSnapshot(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from cargo.image_builder.doctype.pilot_image_snapshot_app.pilot_image_snapshot_app import (
			PilotImageSnapshotApp,
		)

		built_at: DF.Datetime | None
		error: DF.LongText | None
		pilot_image: DF.Link
		required_apps: DF.Table[PilotImageSnapshotApp]
		signup_app: DF.Data | None
		snapshot_id: DF.Data | None
		status: DF.Literal["Pending", "Snapshotting", "Available", "Failed"]
	# end: auto-generated types

	"""One Atlas image taken off a Pilot Image build, with the apps it has turned on."""

	@property
	def supports_app_toggle(self) -> bool:
		# A Version, not the version string: a string is never in a spec, and says so silently.
		return semvar.Version(
			frappe.db.get_value("Pilot Image", self.pilot_image, "frappe_version")
		) in semvar.SimpleSpec(
			frappe.db.get_single_value("Cargo Settings", "version_supporting_app_toggle", cache=True)
		)

	def run_app_prerequisite(self, machine: MachineDoc, image: "PilotImage") -> None:
		"""Leave the site running this snapshot's apps and nothing else."""
		if not self.required_apps:
			return

		builder = Builder()
		private_key = machine.get_password("ssh_private_key")
		required = [row.app for row in self.required_apps]

		if self.supports_app_toggle:
			# Install all apps on site (if not already installed) and disable all apps except the required_apps
			installed = [row.app for row in image.get_required_apps()]
			others = [app for app in installed if app not in required]
			builder.change_site_apps(machine.address, private_key, "install", installed)
			if others:
				builder.change_site_apps(machine.address, private_key, "disable", others)
		else:
			# Install the signup app and what it requires on site and ensure nothing else is installed.
			builder.change_site_apps(machine.address, private_key, "install", required)
			builder.change_site_apps(machine.address, private_key, "verify", ["frappe", *required])

	def run_app_post_requisite(self, machine: MachineDoc, image: "PilotImage") -> None:
		"""Take this snapshot's apps back off the site, so the next snapshot starts from a bare site."""
		if not self.required_apps:
			return

		builder = Builder()
		private_key = machine.get_password("ssh_private_key")
		required = [row.app for row in self.required_apps]

		if self.supports_app_toggle:
			# Only this snapshot's apps are enabled, so disabling them disables all the apps on the site.
			builder.change_site_apps(machine.address, private_key, "disable", required)
		else:
			# Uninstall the signup app and what it requires from site and ensure nothing else is installed.
			builder.change_site_apps(machine.address, private_key, "uninstall", required)
			builder.change_site_apps(machine.address, private_key, "verify", ["frappe"])

	def get_atlas_tags(self, image: "PilotImage") -> dict[str, str]:
		"""The snapshot's specification, which is what a search at Atlas asks for."""
		tags = {
			"purpose": "pilot",
			"pilot_version": image.pilot_version,
			"frappe_version": image.frappe_branch,
			"has_site": str(int(image.image_type != "Base")),
			"has_apps": str(int(bool(self.signup_app))),
			**PILOT_IMAGE_OS_TAGS,
		}
		if self.signup_app:
			tags["app"] = self.signup_app

		return tags

	def take(self, machine: MachineDoc, image: "PilotImage") -> None:
		"""Put the site in this snapshot's state, photograph the machine, then undo that state.
		Atlas answers once the copy is staged, so the machine is free to change for the next
		snapshot."""
		# The snapshot's status is already set by parent PilotImage.mark_snapshotting.
		self.run_app_prerequisite(machine, image)
		Builder().flush_build_machine(machine.address, machine.get_password("ssh_private_key"))

		title = f"{self.pilot_image}-{self.signup_app}" if self.signup_app else self.pilot_image
		self.snapshot_id = machine.snapshot(title, self.get_atlas_tags(image))
		self.built_at = now_datetime()
		self.status = "Available"
		self.save()

		self.run_app_post_requisite(machine, image)
