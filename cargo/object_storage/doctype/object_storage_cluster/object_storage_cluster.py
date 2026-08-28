# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

import typing

import frappe
from frappe import _
from frappe.model.document import Document

from cargo.object_storage.atlas_client import AtlasClient
from cargo.object_storage.central_client import CentralClient
from cargo.object_storage.client_models import GATEWAY, STORAGE, NodeSpec, PlacementGroupSchema

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings


class ObjectStorageCluster(Document):
	"""One Garage cluster: the machines it needs, and the secrets they run on.

	Ask Atlas for machines, wait for them to boot, ask Central for the secrets. Configuring
	the machines is not implemented yet.
	"""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		admin_token: DF.Password | None
		base_image: DF.Data
		cpu: DF.Int
		disk_gb: DF.Int
		error: DF.SmallText | None
		metrics_token: DF.Password | None
		partition_count: DF.Int
		ram_gb: DF.Int
		region: DF.Data
		replication_factor: DF.Int
		rpc_secret: DF.Password | None
		status: DF.Literal["Draft", "Pending", "Machines Ready", "Credentials Minted", "Failed"]
		storage_count: DF.Int
		strategy: DF.Literal["partition", "spread", "pack"]
		topology_key: DF.Data | None
	# end: auto-generated types

	def validate(self) -> None:
		if self.replication_factor < 1:
			frappe.throw(_("Replication factor must be at least 1."))
		if self.storage_count < self.replication_factor:
			frappe.throw(
				_(
					f"{self.storage_count} storage nodes cannot satisfy a replication factor "
					f"of {self.replication_factor}."
				)
			)

	@property
	def instance_count(self) -> int:
		"""Storage nodes plus the one gateway."""
		return self.storage_count + 1

	def placement(self) -> PlacementGroupSchema:
		"""The spread this cluster wants."""
		return PlacementGroupSchema(
			strategy=self.strategy,
			topology_key=self.topology_key,
			partition_count=self.partition_count,
			instance_count=self.instance_count,
			spec=NodeSpec(cpu=self.cpu, ram_gb=self.ram_gb, disk_gb=self.disk_gb),
		)

	def atlas_client(self) -> AtlasClient:
		settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")

		return AtlasClient(
			url=settings.atlas_url,
			token=settings.get_password("atlas_token"),
			public_key=settings.ssh_public_key,
		)

	def central_client(self) -> CentralClient:
		settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")

		return CentralClient(url=settings.central_url, token=settings.get_password("central_token"))

	@frappe.whitelist()
	def provision(self) -> None:
		"""Ask Atlas for the machines. Atlas answers with ids without waiting for boots, so
		this runs inline; the waiting is what gets scheduled."""
		if self.status != "Draft":
			frappe.throw(_(f"This cluster is already {self.status}."))

		self.request_machines()
		self.mark("Pending")

	def request_machines(self) -> None:
		"""Ask Atlas for the machines and record one `Machine` per VM id it returns."""
		try:
			vm_ids = self.atlas_client().create_vms(
				title=f"{self.name} object storage",
				placement=self.placement(),
				base_image=self.base_image,
			)
		except Exception as exception:
			self.mark("Failed", error=str(exception))
			raise

		roles = [GATEWAY] + [STORAGE] * self.storage_count
		for vm_id, role in zip(vm_ids, roles, strict=True):
			frappe.get_doc(
				{
					"doctype": "Machine",
					"vm_id": vm_id,
					"reference_doctype": self.doctype,
					"reference_name": self.name,
					"role": role,
					"zone": self.region,
					"status": "Pending",
				}
			).insert(ignore_permissions=True)

	def mark(self, status: str, error: str | None = None) -> None:
		"""Persist a status change."""
		self.status = status
		self.error = error
		self.save(ignore_permissions=True)

	def machine_names(self, status: str | None = None) -> list[str]:
		filters = {"reference_doctype": self.doctype, "reference_name": self.name}
		if status:
			filters["status"] = status

		return frappe.get_all("Machine", filters=filters, pluck="name")

	def sync_machines(self) -> None:
		"""Refresh pending machines, then move the cluster on if they settled."""
		client = self.atlas_client()
		for name in self.machine_names(status="Pending"):
			frappe.get_doc("Machine", name).sync(client)

		self.reload()
		self.refresh_machine_states()

	def refresh_machine_states(self) -> None:
		"""Move the cluster on once its machines have settled.

		Ready only when every machine is Running: a partly up cluster cannot be configured.
		"""
		states = frappe.get_all(
			"Machine",
			filters={"reference_doctype": self.doctype, "reference_name": self.name},
			pluck="status",
		)
		if not states or "Pending" in states:
			return

		failed = [state for state in states if state in ("Broken", "Terminated")]
		if failed:
			self.mark("Failed", error=f"{len(failed)} of {len(states)} machines failed")
			return

		self.mark("Machines Ready")
		self.mint_credentials()

	def mint_credentials(self) -> None:
		"""Ask Central for the secrets every node of this cluster boots with."""
		if self.status != "Machines Ready":
			return

		try:
			tokens = self.central_client().get_required_credentials(
				region=self.region, vm_ids=self.machine_names()
			)
		except Exception as exception:
			self.mark("Failed", error=str(exception))
			raise

		self.update(tokens)
		self.mark("Credentials Minted")


def sync_pending_machines() -> None:
	"""Walk every cluster still waiting on Atlas. Scheduled in `hooks.py`."""
	waiting = frappe.get_all(
		"Machine",
		filters={"reference_doctype": "Object Storage Cluster", "status": "Pending"},
		pluck="reference_name",
		distinct=True,
	)
	for name in waiting:
		frappe.get_doc("Object Storage Cluster", name).sync_machines()
