# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

import typing
from functools import cached_property

import frappe
from frappe import _
from frappe.utils import now_datetime

from cargo.cargo.doctype.machine.machine import DEAD_MACHINE_STATES
from cargo.cargo.doctype.machine.machine import Machine as MachineDoc
from cargo.client_models import GATEWAY, STORAGE, NodeSpec, Role
from cargo.object_storage.garage.setup import Setup
from cargo.proxy_client import ProxyClient, ProxyError
from cargo.ssh import OutputLog
from cargo.workflow_engine.doctype.press_workflow.decorators import flow, task
from cargo.workflow_engine.doctype.press_workflow.workflow_builder import WorkflowBuilder

if typing.TYPE_CHECKING:
	from frappe.integrations.doctype.webhook.webhook import Webhook

	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings

# Garage wants a 32-byte hex string for its rpc_secret, which is 64 characters of one.
SECRET_LENGTH = 64
WEBHOOK_ENDPOINT = "/api/method/central.api.cargo_webhooks.object_storage_cluster_webhook"
# The two states worth a call: the cluster may be used, or it may not.
REPORTED_STATUSES = ("Active", "Failed")
CLUSTER_SECRETS = ("rpc_secret", "admin_token", "metrics_token")
PROXY_SITE_NAMES = ("s3-svc", "s3-admin-svc")

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
		metadata_dir: DF.Data
		metrics_token: DF.Password | None
		partition_count: DF.Int
		replication_factor: DF.Int
		rpc_port: DF.Int
		rpc_secret: DF.Password | None
		s3_port: DF.Int
		setup_log: DF.Code | None
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

	@property
	def gateway_address(self) -> str:
		"""Where this cluster answers: every S3 and admin call goes through the gateway."""
		gateway = self.gateway_node
		if not gateway:
			frappe.throw(_("This cluster has no gateway to reach it at."))

		if not gateway.address:
			frappe.throw(_("This cluster's gateway has not booted yet."))

		return gateway.address

	@cached_property
	def garage_setup(self) -> Setup:
		"""This cluster's Garage setup client"""
		return Setup(self)

	@property
	def proxy_domains(self) -> tuple[str, ...]:
		wildcard_domain = frappe.db.get_single_value("Cargo Settings", "wildcard_domain", cache=True)
		if not wildcard_domain:
			frappe.throw(_("Wildcard Domain must be set in Cargo Settings."))

		return tuple(f"{site_name}.{wildcard_domain}" for site_name in PROXY_SITE_NAMES)

	def validate(self) -> None:
		if self.status == "Active":
			ensure_no_other_active_cluster(self)

	def before_insert(self) -> None:
		if all(self.get(name) for name in CLUSTER_SECRETS):
			return

		for name in CLUSTER_SECRETS:
			if not self.get(name):
				self.update({name: frappe.generate_hash(length=SECRET_LENGTH)})

	def after_insert(self) -> None:
		"""Ensure webhook for this cluster is configured"""
		configure_storage_cluster_webhook(self)

	def request_machine(self, role: Role, cpu: int, ram_gb: int, disk_gb: int) -> Machine:
		"""Record one machine and ask Atlas to build it. Throws, rolling the record back."""
		spec = NodeSpec(role=role, cpu=cpu, ram_gb=ram_gb, disk_gb=disk_gb)

		return MachineDoc.request(
			self,
			spec,
			base_image=self.base_image,
			zone=self.region,
		)

	@frappe.whitelist()
	def add_gateway_node(self, cpu: int, ram_gb: int, disk_gb: int) -> None:
		"""Can add a gateway node to this cluster? Throws if not."""
		can_add_gateway_node(self)

		machine: Machine = self.request_machine(cpu=cpu, ram_gb=ram_gb, disk_gb=disk_gb, role=GATEWAY)
		self.append("machines", {"machine": machine.name, "role": GATEWAY})
		self.save()

	@frappe.whitelist()
	def add_storage_node(self, cpu: int, ram_gb: int, disk_gb: int) -> None:
		"""Add a storage node to this cluster."""
		can_add_storage_node(self)

		machine: Machine = self.request_machine(cpu=cpu, ram_gb=ram_gb, disk_gb=disk_gb, role=STORAGE)
		self.append("machines", {"machine": machine.name, "role": STORAGE})
		self.save()

	@frappe.whitelist()
	def setup(self) -> None:
		"""Set up what's not setup yet. THat's it idempotently called by the user whenever ready from desk."""
		can_trigger_setup(self)

		registered_nodes = self.garage_setup.healthy_nodes()
		if len(registered_nodes) == len(self.all_nodes):
			if self.publish_proxy_routes():
				self.mark_cluster_status("Active", None)
			return

		self.clear_logs()
		self.mark_cluster_status("Setting Up", None)
		# Here we will start a flow of triggers.
		self._setup.run_as_workflow()

	@flow
	def _setup(self) -> None:
		machines_to_setup = self.discover_machines_to_setup()
		failed_storage_node_setup = []
		for machine in machines_to_setup:
			machine_doc = frappe.get_doc("Machine", machine)
			was_successful = self.start_setup_on_machine(machine_doc)

			# The machines are left alone: setup is idempotent, so a retry picks up whatever
			# has not joined. Releasing them is the operator's call.
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

		self.record_cluster_peers()
		self.apply_layout()
		if not self.verify_connected_nodes():
			return
		if not self.publish_proxy_routes():
			return

		self.mark_cluster_status("Active", None)

	@task
	def discover_machines_to_setup(self) -> list[str]:
		"""Gateway first: every other node reaches the cluster through its admin API, so one
		set up before it has nothing to join."""
		healthy_nodes = self.garage_setup.healthy_nodes()
		machines_to_setup = [machine for machine in self.all_nodes if machine.name not in healthy_nodes]

		return [
			machine.name for machine in sorted(machines_to_setup, key=lambda machine: machine.role != GATEWAY)
		]

	@task
	def start_setup_on_machine(self, machine: Machine) -> bool:
		"""Install Garage on one machine and fold it into the cluster."""
		with OutputLog(self, "setup_log", append=True) as log:
			try:
				garage = self.garage_setup
				garage.setup_machine(garage.machine(machine.name), on_output=log.write)
			except Exception:
				frappe.log_error(
					title=f"{machine.name} failed to set up",
					message=frappe.get_traceback(with_context=True),
				)
				return False

		return True

	@task
	def record_cluster_peers(self) -> None:
		"""Give every node that joined the same peers, now that they all exist."""
		garage = self.garage_setup
		connected = garage.get_connected_nodes()
		if not connected.peers:
			return

		for machine in garage.machines:
			if machine["name"] in connected.machines:
				garage.record_peers(machine, connected.peers)

	@task
	def apply_layout(self) -> None:
		"""Give every node that joined its place. Part of setting up, not a step of its own:
		a joined node carries no storage role until this lands, so a cluster without it
		holds nothing."""
		self.garage_setup.apply_staged_layout()

	@task
	def verify_connected_nodes(self) -> bool:
		"""What actually joined, as Garage sees it. Health labels the rest; this only decides
		whether the cluster came up at all."""
		healthy_nodes = self.garage_setup.healthy_nodes()
		joined_storage = [node for node in self.storage_nodes if node.name in healthy_nodes]

		if len(joined_storage) < self.replication_factor:
			self.mark_cluster_status(
				"Failed",
				_("Only {0} storage nodes joined the cluster, {1} needed for a full copy.").format(
					len(joined_storage), self.replication_factor
				),
			)
			return False

		return True

	@task
	def publish_proxy_routes(self) -> bool:
		"""Publish the regional S3 routes to this cluster's gateway."""
		try:
			client = ProxyClient.from_settings()
			for domain in self.proxy_domains:
				client.map_domain(domain, self.gateway_address)
		except (ProxyError, frappe.ValidationError) as error:
			self.mark_cluster_status("Failed", _("Proxy route setup failed: {0}").format(error))
			return False

		return True

	@frappe.whitelist()
	def release_machines(self, machines: list[str]) -> None:
		"""Hand the named machines back to Atlas and drop them from this cluster."""
		can_release_machines(self, machines)

		named = set(machines)
		failed_terminations = []
		for name in named:
			machine_doc: Machine = frappe.get_doc("Machine", name)
			terminated = machine_doc.terminate()
			if not terminated:
				failed_terminations.append(name)

		self.machines = [
			machine
			for machine in self.machines
			if machine.machine not in named or machine.machine in failed_terminations
		]

		self.save()

	def sync_machines(self) -> None:
		"""What this cluster's machines settling means for it. Their state is already
		recorded; `sync_pending_machines` calls this once it changes."""
		# A cluster being built has promised nothing yet!
		if not self.is_live:
			return

		# Gateway machine dead?
		if self.gateway_node and self.gateway_node.status in DEAD_MACHINE_STATES:
			self.mark_cluster_status("Failed", _("Gateway machine is dead."))
			return

		# Machines less than the replication factor are dead?
		if (
			len([node for node in self.storage_nodes if node.status not in DEAD_MACHINE_STATES])
			< self.replication_factor
		):
			self.mark_cluster_status(
				"Failed", _("Not enough storage nodes are alive to satisfy the replication factor.")
			)
			return

	@property
	def is_live(self) -> bool:
		"""A cluster that has served once. Past that, losing a machine is a real failure."""
		return bool(self.activated_on)

	def mark_cluster_status(self, status: str, reason: str | None = None) -> None:
		"""Mark the cluster's status and reason."""
		if status == "Active" and not self.activated_on:
			self.activated_on = now_datetime()

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
	if cluster.status == "Setting Up":
		frappe.throw(_("Cannot add storage node to a cluster that is setting up."))


def can_trigger_setup(cluster: ObjectStorageCluster) -> None:
	"""If less than required amount of machines are ready to setup, throw."""
	ensure_no_other_active_cluster(cluster)

	if not cluster.gateway_node or cluster.gateway_node.status != "Running":
		frappe.throw(_("This cluster needs a running gateway node before it can be set up."))

	num_running_storage_nodes = len([node for node in cluster.storage_nodes if node.status == "Running"])
	if not num_running_storage_nodes >= cluster.replication_factor:
		frappe.throw(
			_("Not enough running storage nodes to setup the cluster. Required: {0}, running: {1}").format(
				cluster.replication_factor, num_running_storage_nodes
			)
		)


def ensure_no_other_active_cluster(cluster: ObjectStorageCluster) -> None:
	# Check-then-act: an operator activates a cluster by hand, so two at once is not a real race.
	active_cluster = frappe.db.exists(
		"Object Storage Cluster",
		{"status": "Active", "name": ("!=", cluster.name)},
	)
	if active_cluster:
		frappe.throw(_("Object Storage Cluster {0} is already Active.").format(active_cluster))


def can_release_machines(cluster: ObjectStorageCluster, machines: list[str]) -> None:
	"""Whether the named machines can be released from this cluster."""
	cluster.check_permission("write")

	if cluster.status == "Setting Up":
		frappe.throw(_("Cannot release machines from a cluster that is setting up."))

	named = set(machines or [])
	if not named:
		frappe.throw(_("Name the machines to release."))

	unknown = named - {row.machine for row in cluster.machines}
	if unknown:
		frappe.throw(_("{0} is not a machine of this cluster.").format(", ".join(sorted(unknown))))

	# Nothing can have joined yet: no gateway to join through, or no secrets to join with.
	if not cluster.gateway_node or not all(
		cluster.get_password(name, raise_exception=False) for name in CLUSTER_SECRETS
	):
		return

	joined = cluster.garage_setup.healthy_nodes()
	if cluster.is_live:
		roles = {row.machine: row.role for row in cluster.machines}
		if any(roles[name] == GATEWAY for name in named):
			frappe.throw(_("Cannot release the gateway machine from a live cluster."))

		if not joined:
			frappe.throw(_("Unable to release nodes currently garage is blocked."))

	serving = sorted(named & joined)
	if serving:
		frappe.throw(
			_("{0} joined this cluster. Releasing is for machines that failed to set up.").format(
				", ".join(serving)
			)
		)


def configure_storage_cluster_webhook(cluster: ObjectStorageCluster) -> None:
	"""Point a Frappe Webhook at Central so this cluster reports its own status changes."""
	settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")
	if not settings.central_url:
		raise frappe.ValidationError(_("Central URL must be set in Cargo Settings to configure webhook."))

	secret = settings.get_password("central_webhook_secret", raise_exception=True)
	name = f"object_storage_cluster-{cluster.name}"
	webhook: Webhook = (
		frappe.get_doc("Webhook", name) if frappe.db.exists("Webhook", name) else frappe.new_doc("Webhook")
	)
	webhook.name = name
	# TODO:  Once the proxy is layouted we can simply add a field in osc for gateway node address (domain)
	# And that is going to be our service_endpoint.
	webhook.update(
		{
			"webhook_doctype": cluster.doctype,
			"webhook_docevent": "on_update",
			"request_url": settings.central_url.rstrip("/") + WEBHOOK_ENDPOINT,
			"request_method": "POST",
			"request_structure": "JSON",
			"condition": f"doc.status in {REPORTED_STATUSES}",
			"webhook_json": frappe.as_json(
				{
					"region": settings.region,
					"region_id": settings.region_id,
					"service": "storage",
					"status": "{{ doc.status }}",
					"service_endpoint": "<TBD>",
				}
			),
			"enable_security": True,
			"webhook_secret": secret,
			"enabled": True,
		}
	)
	webhook.save(ignore_permissions=True)
