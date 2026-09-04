# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

import typing
from functools import cached_property

import frappe
from frappe import _

from cargo.central_client import CentralClient
from cargo.object_storage.client_models import GATEWAY, STORAGE
from cargo.object_storage.credentials import REQUIRED_CREDENTIALS
from cargo.object_storage.doctype.object_storage_cluster.setup import ClusterSetup
from cargo.object_storage.machines import DEAD_STATES, MachineFleet
from cargo.ssh import OutputLog
from cargo.workflow_engine.doctype.press_workflow.decorators import flow, task
from cargo.workflow_engine.doctype.press_workflow.workflow_builder import WorkflowBuilder

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.machine.machine import Machine


class ObjectStorageCluster(WorkflowBuilder):
	"""One Garage cluster: the machines it needs, and the secrets they run on."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from cargo.cargo.doctype.object_storage_node.object_storage_node import ObjectStorageNode

		activated_on: DF.Datetime | None
		admin_port: DF.Int
		admin_token: DF.Password | None
		base_domain: DF.Data
		base_image: DF.Data
		data_dir: DF.Data
		error: DF.LongText | None
		garage_arch: DF.Data
		garage_binary: DF.Data
		garage_version: DF.Data
		health_reason: DF.Data | None
		health: DF.Literal["Unknown", "Healthy", "Degraded", "Critical"]
		k2v_port: DF.Int
		machines: DF.Table[ObjectStorageNode]
		metadata_bucket: DF.Data | None
		metadata_bucket_access_key: DF.Data | None
		metadata_bucket_secret_key: DF.Password | None
		metadata_dir: DF.Data
		metrics_token: DF.Password | None
		partition_count: DF.Int
		replication_factor: DF.Int
		rpc_port: DF.Int
		rpc_secret: DF.Password | None
		s3_port: DF.Int
		setup_log: DF.Code | None
		ssh_private_key: DF.Password | None
		ssh_public_key: DF.SmallText | None
		status: DF.Literal["Draft", "Setting Up", "Active", "Failed"]
		strategy: DF.Literal["partition", "spread", "pack"]
		topology_key: DF.Data | None
		web_port: DF.Int
	# end: auto-generated types

	@property
	def region(self) -> str:
		"""One Cargo to a region, so Cargo Settings owns it and no cluster carries its own."""
		return frappe.db.get_single_value("Cargo Settings", "region", cache=True)

	@cached_property
	def gateway_node(self) -> Machine | None:
		"""The one machine that has the gateway role."""
		return next(
			(frappe.get_doc("Machine", machine) for machine in self.machines if machine.role == GATEWAY), None
		)

	@cached_property
	def storage_nodes(self) -> list[Machine]:
		"""Every machine that has the storage role."""
		return [frappe.get_doc("Machine", machine) for machine in self.machines if machine.role == STORAGE]

	@cached_property
	def all_nodes(self) -> list[Machine]:
		"""All machines in this cluster."""
		return [frappe.get_doc("Machine", machine) for machine in self.machines]

	@cached_property
	def fleet(self) -> MachineFleet:
		"""The fleet of machines that belong to this cluster."""
		return MachineFleet(self)

	@cached_property
	def garage(self) -> ClusterSetup:
		"""The cluster setup helper."""
		return ClusterSetup(self)

	@frappe.whitelist()
	def add_gateway_node(self, cpu: int, ram_gb: int, disk_gb: int) -> None:
		"""Can add a gateway node to this cluster? Throws if not."""
		can_add_gateway_node(self)

		machine: Machine = self.fleet.request(
			cpu=cpu,
			ram_gb=ram_gb,
			disk_gb=disk_gb,
			role=GATEWAY,
		)
		self.append("machines", {"machine": machine.name, "role": GATEWAY})
		self.save()

	@frappe.whitelist()
	def add_storage_node(self, cpu: int, ram_gb: int, disk_gb: int) -> None:
		"""Add a storage node to this cluster."""
		can_add_storage_node(self)

		machine: Machine = self.fleet.request(
			cpu=cpu,
			ram_gb=ram_gb,
			disk_gb=disk_gb,
			role=STORAGE,
		)
		self.append("machines", {"machine": machine.name, "role": STORAGE})
		self.save()

	@frappe.whitelist()
	def setup(self) -> None:
		"""Set up what's not setup yet. THat's it idempotently called by the user whenever ready from desk."""
		can_trigger_setup(self)

		self.mint_credentials_if_needed()
		registered_nodes = self.garage.healthy_nodes()
		if len(registered_nodes) == len(self.all_nodes):
			self.mark_cluster_status("Active", None)
			return

		self.clear_logs()
		self.mark_cluster_status("Setting Up", None)
		# Here we will start a flow of triggers.
		self._setup.run_as_workflow()

	@frappe.whitelist()
	def apply_layout(self) -> None:
		"""Apply the layout to the cluster. This is idempotent and can be called at any time."""
		self.garage.apply_layout()

	@flow
	def _setup(self) -> None:
		machines_to_setup = self.discover_machines_to_setup()
		failed_storage_node_setup = []
		for machine in machines_to_setup:
			machine_doc = frappe.get_doc("Machine", machine)
			was_successful = self.start_setup_on_machine(machine_doc)

			# We don't care about anything here just make as failure and move on
			if not was_successful and machine_doc.role == GATEWAY:
				self.mark_cluster_status("Failed", _("Gateway machine failed to setup."))
				return

			if not was_successful and machine_doc.role == STORAGE:
				failed_storage_node_setup.append(machine_doc.name)

			if len(self.all_nodes) - len(failed_storage_node_setup) < self.replication_factor:
				self.mark_cluster_status(
					"Failed",
					_(
						"Not enough storage nodes were setup successfully to satisfy the "
						"replication factor. Failed nodes: {0}"
					).format(", ".join(failed_storage_node_setup)),
				)
				return

		self.release_failed_machines(failed_storage_node_setup)
		self.mark_cluster_status("Active", None)

	@task
	def discover_machines_to_setup(self) -> list[str]:
		healthy_nodes = self.garage.healthy_nodes()
		machines_to_setup = [machine.name for machine in self.all_nodes if machine.name not in healthy_nodes]
		return machines_to_setup

	@task
	def start_setup_on_machine(self, machine: Machine) -> bool:
		"""Install Garage on one machine and fold it into the cluster."""
		with OutputLog(self, "setup_log", append=True) as log:
			try:
				setup = ClusterSetup(self, on_output=log.write)
				setup.setup_machine(setup.machine(machine.name))
			except Exception:
				frappe.log_error(
					title=f"{machine.name} failed to set up",
					message=frappe.get_traceback(with_context=True),
				)
				return False

		return True

	@task
	def release_failed_machines(self, failed_machines: list[str]) -> None:
		"""Release the failed machines back to the fleet."""
		for name in failed_machines:
			self.fleet.terminate(frappe.get_doc("Machine", name))
			# Just remove from the cluster's list of machines, don't delete the machine record itself.
			self.machines = [row for row in self.machines if row.machine != name]

		self.save()

	def sync_machines(self) -> None:
		"""Sync the machines in this cluster with the actual machines."""
		machine_states = self.fleet.sync()

		if not machine_states:
			return

		# Gateway machine dead?
		if self.gateway_node and self.gateway_node.status in DEAD_STATES:
			self.mark_cluster_status("Failed", _("Gateway machine is dead."))
			return

		# Machines less than the replication factor are dead?
		if (
			len([node for node in self.storage_nodes if node.status not in DEAD_STATES])
			< self.replication_factor
		):
			self.mark_cluster_status(
				"Failed", _("Not enough storage nodes are alive to satisfy the replication factor.")
			)
			return

	def mint_credentials_if_needed(self) -> None:
		"""Mint this cluster's secrets if it has none. Setup calls it first: nothing can reach
		a node without them, and Central answers the same secrets for a region every time."""
		if not self.admin_token or not self.rpc_secret or not self.metrics_token:
			machine_ids = [machine.vm_id for machine in self.all_nodes]
			try:
				tokens = CentralClient.from_settings().get_required_credentials(
					region=self.region, vm_ids=machine_ids, required=REQUIRED_CREDENTIALS
				)
			except Exception:
				frappe.log_error(
					title=f"{self.name} could not mint credentials",
					message=frappe.get_traceback(with_context=True),
				)
				frappe.throw(_("Failed to mint credentials for this cluster. Please try again later."))

			self.update(
				{
					"admin_token": tokens["admin_token"],
					"rpc_secret": tokens["rpc_secret"],
					"metrics_token": tokens["metrics_token"],
				}
			)
			self.save()

	def mark_cluster_status(self, status: str, reason: str | None = None) -> None:
		"""Mark the cluster's status and reason."""
		self.status = status
		self.error = reason
		self.save()

	def clear_logs(self):
		self.setup_log = None


def can_add_gateway_node(cluster: ObjectStorageCluster) -> None:
	"""Whether this cluster can add a gateway node."""
	if cluster.gateway_node:
		frappe.throw(_("This cluster already has a gateway node."))


def can_add_storage_node(cluster: ObjectStorageCluster) -> None:
	"""Whether this cluster can add a storage node."""
	if cluster.status in ["Failed", "Setting Up"]:
		frappe.throw(_("Cannot add storage node to a cluster that is failed or setting up."))


def can_trigger_setup(cluster: ObjectStorageCluster) -> None:
	"""If less than required amount of machines are ready to setup, throw."""
	num_healthy_storage_nodes = len([node for node in cluster.storage_nodes if node.status == "Running"])
	if not num_healthy_storage_nodes >= cluster.replication_factor:
		frappe.throw(
			_("Not enough healthy storage nodes to setup the cluster. Required: {0}, healthy: {1}").format(
				cluster.replication_factor, num_healthy_storage_nodes
			)
		)

	if not cluster.gateway_node:
		frappe.throw(
			_("This cluster does not have a gateway node. Please add one before setting up the cluster.")
		)
