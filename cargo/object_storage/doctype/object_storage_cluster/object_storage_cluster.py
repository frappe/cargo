# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

import typing
from functools import cached_property

import frappe
from frappe import _
from frappe.utils import now_datetime

from cargo.central_client import CentralClient
from cargo.object_storage.client_models import GATEWAY, STORAGE
from cargo.object_storage.credentials import REQUIRED_CREDENTIALS
from cargo.object_storage.doctype.object_storage_cluster.setup import ClusterSetup
from cargo.object_storage.machines import DEAD_STATES, MachineFleet
from cargo.ssh import OutputLog, create_keypair
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

	def before_insert(self) -> None:
		"""One keypair per cluster, made here so nobody has to paste one in."""
		if not self.ssh_public_key:
			self.ssh_public_key, self.ssh_private_key = create_keypair(self.name or self.region)

	@property
	def region(self) -> str:
		"""One Cargo to a region, so Cargo Settings owns it and no cluster carries its own."""
		return frappe.db.get_single_value("Cargo Settings", "region", cache=True)

	@cached_property
	def gateway_node(self) -> Machine | None:
		"""The one machine that has the gateway role."""
		return next(
			(frappe.get_doc("Machine", row.machine) for row in self.machines if row.role == GATEWAY), None
		)

	@cached_property
	def storage_nodes(self) -> list[Machine]:
		"""Every machine that has the storage role."""
		return [frappe.get_doc("Machine", row.machine) for row in self.machines if row.role == STORAGE]

	@cached_property
	def all_nodes(self) -> list[Machine]:
		"""All machines in this cluster."""
		return [frappe.get_doc("Machine", row.machine) for row in self.machines]

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
				self.release_failed_machines([node.name for node in self.all_nodes])
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
		self.record_cluster_peers()
		self.verify_connected_nodes()

	@task
	def discover_machines_to_setup(self) -> list[str]:
		"""Gateway first: every other node reaches the cluster through its admin API, so one
		set up before it has nothing to join."""
		healthy_nodes = self.garage.healthy_nodes()
		machines_to_setup = [machine for machine in self.all_nodes if machine.name not in healthy_nodes]

		return [
			machine.name for machine in sorted(machines_to_setup, key=lambda machine: machine.role != GATEWAY)
		]

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
	def record_cluster_peers(self) -> None:
		"""Give every node the same peers, now that they all exist"""
		setup = ClusterSetup(self)
		peers = setup.peers()
		if not peers:
			return

		for machine in setup.machines:
			setup.record_peers(machine, peers)

	@task
	def verify_connected_nodes(self) -> None:
		"""What actually joined, as Garage sees it. Health labels the rest; this only decides
		whether the cluster came up at all."""
		healthy_nodes = self.garage.healthy_nodes()
		joined_storage = [node for node in self.storage_nodes if node.name in healthy_nodes]

		if len(joined_storage) < self.replication_factor:
			self.mark_cluster_status(
				"Failed",
				_("Only {0} storage nodes joined the cluster, {1} needed for a full copy.").format(
					len(joined_storage), self.replication_factor
				),
			)
			return

		# Short of a node but able to serve: Active, and health reports it as degraded.
		self.mark_cluster_status("Active", None)

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

		# A cluster being built has promised nothing yet!
		if not self.is_live:
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

	def inform_central_of_cluster_health(self, health: typing.Literal["Active", "Failed"]) -> None:
		"""Tell Central whether this region's cluster may be used. Todo: add health reporting system.

		Only a running cluster has endpoints to report, and only it can be asked for the
		gateway address they are built from."""
		endpoints = self.central_endpoints if health == "Active" else {}
		try:
			CentralClient.from_settings().register_cluster(
				region=self.region, active=health == "Active", **endpoints
			)
		except Exception:
			frappe.log_error(
				title=f"{self.name} could not inform Central it is {health}",
				message=frappe.get_traceback(with_context=True),
			)
			frappe.throw(_("Failed to inform Central of this cluster's status. Please try again later."))

	@property
	def central_endpoints(self) -> dict[str, str]:
		"""Where Central reaches this cluster. Every call goes through the gateway, and
		nothing terminates TLS in front of Garage."""
		address = self.fleet.gateway_address

		return {
			"base_url": f"http://{address}:{self.admin_port}",
			"s3_endpoint": f"http://{address}:{self.s3_port}",
			"web_endpoint": f"http://{address}:{self.web_port}",
		}

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

	@property
	def is_live(self) -> bool:
		"""A cluster that has served once. Past that, losing a machine is a real failure."""
		return bool(self.activated_on)

	def mark_cluster_status(self, status: str, reason: str | None = None) -> None:
		"""Mark the cluster's status and reason."""
		if status == "Active" and not self.activated_on:
			self.activated_on = now_datetime()

		if status == "Active" or status == "Failed":
			# Inform on the two most critical states of the cluster.
			self.inform_central_of_cluster_health(status)

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
	if not cluster.gateway_node or cluster.gateway_node.status != "Running":
		frappe.throw(_("This cluster needs a running gateway node before it can be set up."))

	num_running_storage_nodes = len([node for node in cluster.storage_nodes if node.status == "Running"])
	if not num_running_storage_nodes >= cluster.replication_factor:
		frappe.throw(
			_("Not enough running storage nodes to setup the cluster. Required: {0}, running: {1}").format(
				cluster.replication_factor, num_running_storage_nodes
			)
		)
