# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

import typing

import frappe
import semantic_version as semvar
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from cargo.atlas_client import PILOT_IMAGE_OS_TAGS, AtlasClient, AtlasNotFound
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
		if not self.signup_app:
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
		if not self.signup_app:
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
		"""Ask Atlas to photograph the machine as it is now. `complete_if_available` finishes
		the snapshot once Atlas has made the image."""
		Builder().flush_build_machine(machine.address, machine.get_password("ssh_private_key"))

		title = f"{self.pilot_image}-{self.signup_app}" if self.signup_app else self.pilot_image
		self.snapshot_id = machine.snapshot(title, self.get_atlas_tags(image))
		self.save()

	def complete_if_available(self) -> bool:
		"""Mark this snapshot Available once Atlas has made the image. False while Atlas is
		still making it. Throws when Atlas failed it or is removing it."""
		atlas_image = AtlasClient.from_settings().get_snapshot(self.snapshot_id)
		status = atlas_image.get("status")
		if status in ("failed", "deleting", "archived"):
			frappe.throw(
				_("Atlas image {0} is {1}: {2}").format(
					self.snapshot_id, status, atlas_image.get("transfer_error") or _("Atlas gave no reason.")
				)
			)

		if status != "available":
			return False

		self.built_at = now_datetime()
		self.status = "Available"
		self.save()

		return True

	def delete_atlas_image(self, client: AtlasClient) -> bool:
		"""Ask Atlas to delete this snapshot's image, and let go of it only once Atlas says it
		is gone. False while Atlas still has it, so a later call tries again."""
		try:
			client.delete_snapshot(self.snapshot_id)
			status = client.get_snapshot(self.snapshot_id).get("status")
		except AtlasNotFound:
			status = "deleted"
		except Exception:
			frappe.log_error(
				title=f"Could not delete Atlas image {self.snapshot_id} of {self.name}",
				message=frappe.get_traceback(with_context=True),
			)
			return False

		# Atlas reclaims a deleting or archived image itself, and boots neither.
		if status not in ("deleted", "deleting", "archived"):
			return False

		self.status = "Failed"
		self.error = "\n".join(
			filter(None, [self.error, _("Atlas image {0} is deleted.").format(self.snapshot_id)])
		)
		self.snapshot_id = None
		self.save()

		return True
