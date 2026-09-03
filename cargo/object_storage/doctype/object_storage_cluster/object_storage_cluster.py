# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _

from cargo.atlas_client import AtlasClient
from cargo.central_client import CentralClient
from cargo.object_storage.client_models import GATEWAY, STORAGE, NodeSpec, PlacementGroupSchema
from cargo.object_storage.credentials import REQUIRED_CREDENTIALS
from cargo.object_storage.doctype.object_storage_cluster.setup import ClusterSetup
from cargo.service_cluster import ServiceCluster
from cargo.ssh import OutputLog, create_keypair
from cargo.workflow_engine.doctype.press_workflow.decorators import task


class ObjectStorageCluster(ServiceCluster):
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
		error: DF.LongText | None
		garage_arch: DF.Data
		garage_binary: DF.Data
		garage_version: DF.Data
		gateway_disk_gb: DF.Int
		k2v_port: DF.Int
		metadata_bucket: DF.Data | None
		metadata_bucket_access_key: DF.Data | None
		metadata_bucket_secret_key: DF.Password | None
		metadata_dir: DF.Data
		metrics_token: DF.Password | None
		partition_count: DF.Int
		ram_gb: DF.Int
		region: DF.Data
		replication_factor: DF.Int
		rpc_port: DF.Int
		rpc_secret: DF.Password | None
		s3_port: DF.Int
		setup_log: DF.Code | None
		ssh_private_key: DF.Password | None
		ssh_public_key: DF.SmallText | None
		status: DF.Literal[
			"Draft",
			"Pending",
			"Machines Ready",
			"Minting Failed",
			"Credentials Minted",
			"Setting Up",
			"Active",
			"Failed",
		]
		storage_count: DF.Int
		strategy: DF.Literal["partition", "spread", "pack"]
		topology_key: DF.Data | None
		web_port: DF.Int
	# end: auto-generated types

	MINT_FROM = ("Machines Ready", "Minting Failed")

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

	def before_insert(self) -> None:
		"""One keypair per cluster, made here so nobody has to paste one in."""
		if not self.ssh_public_key:
			self.ssh_public_key, self.ssh_private_key = create_keypair(self.name or self.region)

	@frappe.whitelist()
	def provision(self) -> None:
		"""Ask Atlas for the machines. Fast: ids come back without waiting for boots."""
		if self.status != "Draft":
			frappe.throw(_(f"This cluster is already {self.status}."))

		self.request_machines()

	def request_machines(self) -> None:
		"""Record one `Machine` per VM id Atlas returns."""
		placement = self.placement()
		roles = placement.roles()

		try:
			vm_ids = AtlasClient.from_settings().create_vms(
				title=f"{self.name} object storage",
				placement=placement,
				base_image=self.base_image,
				public_key=self.ssh_public_key,
			)
		except Exception:
			self.mark("Failed", error=frappe.get_traceback(with_context=True))
			return

		if len(vm_ids) != len(roles):
			# We should request deletion of the VMs created before marking the cluster failed.
			self.mark(
				"Failed",
				error=f"Asked Atlas for {len(roles)} machines and got {len(vm_ids)}. "
				f"These are running and untracked: {', '.join(vm_ids) or 'none'}",
			)
			return

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

		self.mark("Pending")

	def machine_names(self, status: str | None = None) -> list[str]:
		filters = {"reference_doctype": self.doctype, "reference_name": self.name}
		if status:
			filters["status"] = status

		return frappe.get_all("Machine", filters=filters, pluck="name")

	def sync_machines(self) -> None:
		"""Refresh pending machines, then move the cluster on if they settled."""
		client = AtlasClient.from_settings()
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

	@frappe.whitelist()
	def mint_credentials(self) -> None:
		"""Ask Central for the secrets every node of this cluster boots with."""
		if self.status not in self.MINT_FROM:
			return

		try:
			tokens = CentralClient.from_settings().get_required_credentials(
				region=self.region, vm_ids=self.machine_names(), required=REQUIRED_CREDENTIALS
			)
		except Exception:
			self.mark("Minting Failed", error=frappe.get_traceback(with_context=True))
			return

		self.update(tokens)
		self.mark("Credentials Minted")

	def clear_logs(self) -> None:
		self.setup_log = None

	@task
	def terraform(self) -> None:
		"""Install Garage and lay the cluster out"""
		# This will expose our bucket secrets in the log.
		with OutputLog(self, "setup_log") as log:
			setup = ClusterSetup(self, on_output=log.write)
			setup.assign_layout(setup.bootstrap_machines())
			metadata_bucket_info = setup.create_metadata_bucket()

		self.update(
			{
				"metadata_bucket": metadata_bucket_info["name"],
				"metadata_bucket_access_key": metadata_bucket_info["access_key"],
				"metadata_bucket_secret_key": metadata_bucket_info["secret_key"],
			}
		)
		self.save()

	@task
	def verify(self) -> None:
		"""Check every node joined and the layout applied"""
		setup = ClusterSetup(self)
		healthy = setup.healthy_nodes()
		missing = {machine["name"] for machine in setup.machines} - healthy
		if missing:
			frappe.throw(_(f"These nodes have not joined the cluster: {', '.join(sorted(missing))}"))

		if not setup.layout_version():
			frappe.throw(_("The cluster has no applied layout."))

	@task
	def register(self) -> None:
		"""Register the cluster with Central"""
		gateway = self.gateway_address()
		CentralClient.from_settings().register_cluster(
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
		object_storage_cluster: ObjectStorageCluster = frappe.get_doc("Object Storage Cluster", name)
		object_storage_cluster.sync_machines()
