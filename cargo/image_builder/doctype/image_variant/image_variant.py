# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from functools import cached_property
from typing import TypedDict

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from cargo.atlas_client import AtlasClient
from cargo.image_builder.builder import Builder

BUILD_TIMEOUT = 3600
DEAD_STATES = {"Failed", "Error", "Terminated", "Archived", "Broken"}
SITE_DOMAIN = "frappe.cloud"
NAME_LENGTH = 8


class ImageDetail(TypedDict):
	kind: str
	version: str


class ImageVariant(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		admin_password: DF.Password | None
		bench_name: DF.Data | None
		built_at: DF.Datetime | None
		error: DF.LongText | None
		frappe_version: DF.Literal["", "version-15", "version-16", "develop"]
		image: DF.Link
		site: DF.Literal["", "Included", "Not Included"]
		site_name: DF.Data | None
		snapshot_id: DF.Data | None
		ssh_private_key: DF.Password | None
		ssh_public_key: DF.SmallText | None
		status: DF.Literal["Draft", "Provisioning", "Building", "Available", "Failed"]
		temporary_vm_id: DF.Data | None
	# end: auto-generated types

	"""One flavour of a release's image, and the snapshot it produced.

	Each variant builds, fails and retries on its own, so a broken develop build leaves the
	version-16 image alone."""

	def validate(self) -> None:
		"""A dimension is blank, never None: the two would otherwise be different rows to a
		query, and the same flavour could be created twice."""
		self.frappe_version = self.frappe_version or ""
		self.site = self.site or ""

		if self.site == "Included" and not self.frappe_version:
			frappe.throw(frappe._("A site needs a Frappe version to be built against."))

		if frappe.db.exists(
			"Image Variant",
			{
				"image": self.image,
				"frappe_version": self.frappe_version,
				"site": self.site,
				"name": ("!=", self.name),
			},
		):
			frappe.throw(frappe._("This image already has that flavour."))

	@cached_property
	def image_details(self) -> ImageDetail:
		"""The Image's kind and version, read once per instance."""
		return frappe.db.get_value("Image", self.image, ["kind", "version"], as_dict=True, cache=True)

	@property
	def title(self) -> str:
		"""What the build machine and its snapshot are called at Atlas."""
		return "-".join(part for part in (self.image, self.frappe_version) if part)

	@property
	def builder(self) -> Builder:
		return Builder(self.image_details.kind, self.title)

	@property
	def provision_environment(self) -> dict[str, str]:
		"""What this image is, as its kind's script reads it. One branch per kind: the
		variables are the script's, and every kind's script asks for different ones."""
		kind = self.image_details.kind
		if kind == "pilot":
			return self.pilot_environment

		frappe.throw(frappe._("No provisioning environment for {0} images.").format(kind))

	@property
	def pilot_environment(self) -> dict[str, str]:
		"""The Image's own version, not its name: the name carries the kind as a prefix."""
		return {
			"VERSION": self.image_details.version,
			"FRAPPE_VERSION": self.frappe_version,
			"SITE": self.site_name or "",
			"BENCH": self.bench_name,
			"ADMIN_PASSWORD": self.get_password("admin_password"),
		}

	def name_contents(self) -> None:
		"""Name the bench and site this image will carry. Kept once generated, so a rebuild
		does not silently change what is inside the image."""
		self.bench_name = self.bench_name or f"bench-{frappe.generate_hash(length=NAME_LENGTH)}"

		if self.site == "Included" and not self.site_name:
			region = frappe.db.get_single_value("Cargo Settings", "region")
			self.site_name = f"{frappe.generate_hash(length=NAME_LENGTH)}.{region}.{SITE_DOMAIN}"

	@frappe.whitelist()
	def build(self) -> None:
		"""Ask Atlas for a machine to bake. Booting takes minutes, so the scheduler picks it
		up from here rather than this request holding a worker open."""
		if self.status in ("Provisioning", "Building"):
			frappe.throw(frappe._("This variant is already building."))

		self.name_contents()
		public_key, private_key = self.builder.create_keypair()
		self.ssh_public_key = public_key
		self.ssh_private_key = private_key
		self.admin_password = frappe.generate_hash(length=32)
		self.temporary_vm_id = self.builder.provision_build_machine(public_key=public_key)
		self.mark("Provisioning")

	def sync_build_vm(self) -> None:
		"""Move the variant on once its machine is up. Scheduled, one machine at a time."""
		machine = AtlasClient.from_settings().get_vm(self.temporary_vm_id)
		status = machine.get("status")

		if status in DEAD_STATES:
			self.mark("Failed", error=f"Atlas reported {status}")
			return

		if status != "Running" or not machine.get("ipv4_address"):
			return

		self.mark("Building")
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"start_build",
			address=machine["ipv4_address"],
			queue="long",
			timeout=BUILD_TIMEOUT,
		)

	def start_build(self, address: str) -> None:
		"""Install pilot over SSH, snapshot the machine, then throw it away."""
		builder = self.builder
		try:
			builder.run_provision_script_on_build_machine(
				address, self.get_password("ssh_private_key"), self.provision_environment
			)
			snapshot = builder.snapshot_build_machine(self.temporary_vm_id)
		except Exception as exception:
			self.mark("Failed", error=str(exception))
			raise
		finally:
			builder.destroy_build_machine(self.temporary_vm_id)

		self.snapshot_id = snapshot
		self.built_at = now_datetime()
		self.temporary_vm_id = None
		self.mark("Available")

	def mark(self, status: str, error: str | None = None) -> None:
		self.status = status
		self.error = error
		self.save(ignore_permissions=True)


def sync_build_machines() -> None:
	"""Walk every variant still waiting on Atlas. Scheduled in `hooks.py`."""
	waiting = frappe.get_all(
		"Image Variant",
		filters={"status": "Provisioning", "temporary_vm_id": ["is", "set"]},
		pluck="name",
	)
	for name in waiting:
		variant: ImageVariant = frappe.get_doc("Image Variant", name)
		variant.sync_build_vm()
