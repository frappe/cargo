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
from cargo.object_storage.credentials import REQUIRED_CREDENTIALS
from cargo.object_storage.doctype.object_storage_cluster.installer import ClusterSetup

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings


class ObjectStorageCluster(Document):
	"""One Garage cluster: the machines it needs, and the secrets they run on."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		admin_port: DF.Int
		admin_token: DF.Password | None
		base_domain: DF.Data
		base_image: DF.Data
		cpu: DF.Int
		data_dir: DF.Data
		disk_gb: DF.Int
		error: DF.SmallText | None
		garage_arch: DF.Data
		garage_binary: DF.Data
		garage_version: DF.Data
		gateway_disk_gb: DF.Int
		k2v_port: DF.Int
		metadata_dir: DF.Data
		metrics_token: DF.Password | None
		partition_count: DF.Int
		ram_gb: DF.Int
		region: DF.Data
		replication_factor: DF.Int
		rpc_port: DF.Int
		rpc_secret: DF.Password | None
		s3_port: DF.Int
		ssh_private_key: DF.Password
		ssh_public_key: DF.SmallText
		status: DF.Literal["Draft", "Pending", "Machines Ready", "Credentials Minted", "Active", "Failed"]
		storage_count: DF.Int
		strategy: DF.Literal["partition", "spread", "pack"]
		topology_key: DF.Data | None
		web_port: DF.Int
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
		return self.storage_count + 1

	def placement(self) -> PlacementGroupSchema:
		return PlacementGroupSchema(
			strategy=self.strategy,
			topology_key=self.topology_key,
			partition_count=self.partition_count,
			specs=[
				NodeSpec(
					role=GATEWAY, count=1, cpu=self.cpu, ram_gb=self.ram_gb, disk_gb=self.gateway_disk_gb
				),
				NodeSpec(
					role=STORAGE,
					count=self.storage_count,
					cpu=self.cpu,
					ram_gb=self.ram_gb,
					disk_gb=self.disk_gb,
				),
			],
		)

	def atlas_client(self) -> AtlasClient:
		settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")

		return AtlasClient(
			url=settings.atlas_url,
			token=settings.get_password("atlas_token"),
			public_key=self.ssh_public_key,
		)

	def central_client(self) -> CentralClient:
		settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")

		return CentralClient(url=settings.central_url, token=settings.get_password("central_token"))

	@frappe.whitelist()
	def provision(self) -> None:
		"""Ask Atlas for the machines. Fast: ids come back without waiting for boots."""
		if self.status != "Draft":
			frappe.throw(_(f"This cluster is already {self.status}."))

		self.request_machines()
		self.mark("Pending")

	def request_machines(self) -> None:
		"""Record one `Machine` per VM id Atlas returns."""
		placement = self.placement()
		try:
			vm_ids = self.atlas_client().create_vms(
				title=f"{self.name} object storage",
				placement=placement,
				base_image=self.base_image,
			)
		except Exception as exception:
			self.mark("Failed", error=str(exception))
			raise

		for vm_id, role in zip(vm_ids, placement.roles(), strict=True):
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
		"""Move the cluster on once its machines have settled."""
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
				region=self.region, vm_ids=self.machine_names(), required=REQUIRED_CREDENTIALS
			)
		except Exception as exception:
			self.mark("Failed", error=str(exception))
			raise

		self.update(tokens)
		self.mark("Credentials Minted")

	@frappe.whitelist()
	def setup_cluster(self) -> None:
		"""Trigger the actual installation of service on the machines."""
		if self.status != "Credentials Minted":
			frappe.throw(_(f"This cluster is {self.status}, not ready to set up."))

		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"run_setup",
			queue="long",
			timeout=3600,
			job_name=f"Install {self.name}",
		)

	def run_setup(self) -> None:
		"""Install Garage everywhere, lay the cluster out, then hand Central its endpoints."""
		setup = ClusterSetup(self)
		try:
			setup.assign_layout(setup.bootstrap_machines())
			self.register_with_central()
		except Exception as exception:
			self.mark("Failed", error=str(exception))
			raise

		self.mark("Active")

	def register_with_central(self) -> None:
		"""Central holds this cluster's secrets already but has nowhere to send them until
		it knows the gateway's address."""
		gateway = self.gateway_address()
		self.central_client().register_cluster(
			region=self.region,
			base_url=f"http://{gateway}:{self.admin_port}",
			s3_endpoint=f"http://{gateway}:{self.s3_port}",
		)

	def gateway_address(self) -> str:
		address = frappe.db.get_value(
			"Machine",
			{"reference_doctype": self.doctype, "reference_name": self.name, "role": GATEWAY},
			"ipv4_address",
		)
		if not address:
			frappe.throw(_("This cluster has no gateway to reach it at."))

		return address


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
