from __future__ import annotations

import typing
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from typing import TypedDict
from urllib.parse import urlsplit

import frappe
from frappe import _
from frappe.utils.password import get_decrypted_password

from cargo.atlas_client import host_port
from cargo.client_models import GATEWAY, STORAGE
from cargo.object_storage.client import Client, Error
from cargo.ssh import run_over_ssh, script

BINARY_URL = "https://garagehq.deuxfleurs.fr/_releases/{version}/{arch}/garage"
CONF = ("object_storage", "conf", "garage")
NGINX_CONF = ("object_storage", "conf", "nginx", "install.sh")
# Who may say where a request came from. The proxy reaches the gateway over the mesh, and a
# unique-local address cannot arrive from the internet, so no client can forge the header.
TRUSTED_PROXIES = ("127.0.0.1", "::1", "fd00::/8")
GIGABYTE = 1000**3
#: `Machine.name`, e.g. ``OSC-0001-storage-0001``.
MachineName = str
#: ``<node id>@<address>:<rpc port>``, as `garage node id` prints it.
NodeIdentifier = str


class MachineRow(TypedDict):
	"""The `Machine` fields setup reads."""

	name: MachineName
	role: str
	zone: str
	address: str
	disk_size_gb: int


@dataclass(frozen=True)
class ConnectedNodes:
	"""One read of what Garage can see."""

	peers: list[NodeIdentifier]
	machines: set[MachineName]


class Setup(Client):
	"""Setting a cluster's machines up, and asking Garage what it sees."""

	@cached_property
	def machines(self) -> list[MachineRow]:
		"""Every machine that has booted, gateway first: the rest reach the cluster through it."""
		machines = frappe.get_all(
			"Machine",
			filters={
				"reference_doctype": self.cluster.doctype,
				"reference_name": self.cluster.name,
				"status": "Running",
			},
			fields=["name", "role", "zone", "address", "disk_size_gb"],
			order_by="creation",
		)

		return sorted(machines, key=lambda machine: machine["role"] != GATEWAY)

	def key_for(self, machine: MachineRow) -> str:
		"""A machine is reached with the key it was built with. Password fields are not
		columns, so this cannot come off the machine query."""
		return get_decrypted_password("Machine", machine["name"], "ssh_private_key")

	def run(self, machine: MachineRow, script: str, on_output: Callable[[str], None] | None = None) -> str:
		"""Every command a node is given, streamed to `on_output` as it arrives."""
		return run_over_ssh(machine["address"], script, self.key_for(machine), on_output=on_output)

	def layout_version(self) -> int:
		"""The applied layout version, zero if none. Staged changes are a separate field."""
		try:
			return self.layout().get("version", 0)
		except Error:
			return 0

	def get_connected_nodes(self) -> ConnectedNodes:
		"""The nodes Garage can reach, as it addresses them, and whose machines they are."""
		try:
			nodes = self.status().get("nodes") or []
			# A node is joined once it is up and tagged, whether or not the layout carrying
			# that tag has been applied: applying is a separate step.
			staged = self.layout().get("stagedRoleChanges") or []
		except Error:
			return ConnectedNodes(peers=[], machines=set())

		staged_tags = {change["id"]: change.get("tags") or [] for change in staged}
		up = [node for node in nodes if node.get("isUp")]
		tags = set()
		for node in up:
			tags.update((node.get("role") or {}).get("tags", []))
			tags.update(staged_tags.get(node["id"], []))

		return ConnectedNodes(
			peers=[f"{node['id']}@{node['addr']}" for node in up if node.get("addr")],
			machines={machine["name"] for machine in self.machines if machine["name"] in tags},
		)

	def healthy_nodes(self) -> set[MachineName]:
		"""The machine names Garage reports as up, read from the node tags setup assigned."""
		return self.get_connected_nodes().machines

	def machine(self, name: MachineName) -> MachineRow:
		"""One booted machine of this cluster, by name."""
		machine = next((row for row in self.machines if row["name"] == name), None)
		if not machine:
			frappe.throw(_(f"{name} is not a machine of this cluster, or has not booted."))

		return machine

	def node_identifier(
		self, machine: MachineRow, on_output: Callable[[str], None] | None = None
	) -> NodeIdentifier:
		"""Only answers once the node has started, since Garage keys itself on first launch."""
		return self.run(machine, "garage node id -q", on_output).strip().splitlines()[-1]

	@property
	def secrets(self) -> dict[str, str]:
		"""The secrets a node boots with, named in the error when the cluster has none."""
		from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
			CLUSTER_SECRETS,
		)

		found = {name: self.cluster.get_password(name, raise_exception=False) for name in CLUSTER_SECRETS}
		missing = [name for name, value in found.items() if not value]
		if missing:
			frappe.throw(_(f"This cluster has no {', '.join(missing)}. Mint its credentials first."))

		return found

	@cached_property
	def wildcard_domain(self) -> str:
		"""The domain the gateway's subdomains hang off."""
		domain = frappe.db.get_single_value("Cargo Settings", "wildcard_domain")
		if not domain:
			frappe.throw(_("Set a Wildcard Domain in Cargo Settings before setting up a gateway."))

		return domain

	def install_environment(self, machine: MachineRow) -> dict[str, str]:
		"""What a node needs to write its own garage.toml and unit."""
		cluster, secrets = self.cluster, self.secrets

		return {
			"GARAGE_BINARY": cluster.garage_binary,
			"GARAGE_VERSION": cluster.garage_version,
			"BINARY_URL": BINARY_URL.format(version=cluster.garage_version, arch=cluster.garage_arch),
			"METADATA_DIR": cluster.metadata_dir,
			"DATA_DIR": cluster.data_dir,
			"RPC_PUBLIC_ADDR": host_port(machine["address"], cluster.rpc_port),
			"REGION": cluster.region,
			"REPLICATION_FACTOR": cluster.replication_factor,
			"RPC_PORT": cluster.rpc_port,
			"S3_PORT": cluster.s3_port,
			"ADMIN_PORT": cluster.admin_port,
			"RPC_SECRET": secrets["rpc_secret"],
			"ADMIN_TOKEN": secrets["admin_token"],
			"METRICS_TOKEN": secrets["metrics_token"],
		}

	def record_peers(self, machine: MachineRow, peers: list[NodeIdentifier]) -> str:
		"""Where a node looks for the others after a reboot. Nothing restarts to read it."""
		return self.run(
			machine,
			script(*CONF, "set_peers.sh", environment={"BOOTSTRAP_PEERS": " ".join(peers)}),
		)

	def setup_machine(self, machine: MachineRow, on_output: Callable[[str], None] | None = None) -> None:
		"""Install Garage on one machine and fold it into whatever cluster already exists."""
		if on_output:
			on_output(f"\n=== {machine['name']} ({machine['address']}) ===\n")

		self.run(
			machine,
			script(*CONF, "install.sh", environment=self.install_environment(machine)),
			on_output,
		)
		identifier = self.node_identifier(machine, on_output)
		self.connect_nodes([identifier])
		self.stage_role(machine, identifier)

	def stage_role(self, machine: MachineRow, identifier: NodeIdentifier) -> dict:
		"""Write this machine into the next layout. Nothing takes effect until it is applied."""
		role = {
			"id": identifier.split("@")[0],
			"zone": machine["zone"],
			"tags": [machine["name"]],
		}
		if machine["role"] == STORAGE:
			# Per machine: Garage weights a node by its own disk, so the disks may differ.
			role["capacity"] = machine["disk_size_gb"] * GIGABYTE

		return self.assign_roles([role])

	def setup_nginx_on_machine(
		self, machine: MachineRow, on_output: Callable[[str], None] | None = None
	) -> None:
		"""Put nginx on the gateway's port 80 routing to s3 and admin api."""
		if machine["role"] != GATEWAY:
			frappe.throw(
				_("{0} is a {1} node. Only the gateway routes traffic.").format(
					machine["name"], machine["role"]
				)
			)

		self.run(machine, script(*NGINX_CONF, environment=self.nginx_environment()), on_output)

	def nginx_environment(self) -> dict[str, str]:
		"""What the gateway needs to route its two subdomains."""
		return {
			"WILDCARD_DOMAIN": self.wildcard_domain,
			"S3_PORT": self.cluster.s3_port,
			"ADMIN_PORT": self.cluster.admin_port,
			"TRUSTED_PROXIES": " ".join(TRUSTED_PROXIES),
			"CORS_ORIGIN": self.central_origin,
		}

	@property
	def central_origin(self) -> str:
		"""The origin the Central dashboard runs on, the one origin S3 accepts browser calls from."""
		url = urlsplit(frappe.db.get_single_value("Cargo Settings", "central_url") or "")
		if not (url.scheme and url.netloc):
			frappe.throw(_("Central URL must be set in Cargo Settings."))

		return f"{url.scheme}://{url.netloc}"

	def apply_staged_layout(self) -> None:
		"""One version for everything staged, stale roles dropped with it. Garage refuses a
		layout that cannot hold a full copy, so a gateway and its storage nodes have to land
		together."""
		self.stage_stale_role_removals()
		layout = self.layout()
		if layout.get("stagedRoleChanges"):
			self.apply_layout(layout.get("version", 0) + 1)

		# On every run, not only the one that removed a node: a skip that failed or had to
		# wait for another node is tried again.
		self.skip_dead_nodes_left_out_of_layout()

	def stage_stale_role_removals(self) -> list[str]:
		"""Stage the removal of a released machine's node,
		or the old node of a machine that came back with a new node id.
		Returns the node ids staged for removal."""
		# All my machines
		cluster_machines = [row.machine for row in self.cluster.machines]
		# All nodes in the layout.
		layout_nodes = self.get_layout_nodes()
		# All running nodes in the cluster according to garage status
		running_nodes = self.get_running_node_ids()

		machines_with_a_running_node = []
		for node_id, machine in layout_nodes:
			if node_id in running_nodes:
				machines_with_a_running_node.append(machine)

		stale_nodes = []
		for node_id, machine in layout_nodes:
			if machine not in cluster_machines:
				# The machine was released.
				stale_nodes.append(node_id)
			elif node_id not in running_nodes and machine in machines_with_a_running_node:
				# This node id is not running however, machine does have another running node id.
				# Which means machine came back with a new node id. So we can remove this old node id.
				stale_nodes.append(node_id)

		if stale_nodes:
			removals = [{"id": node_id, "remove": True} for node_id in stale_nodes]
			self.assign_roles(removals)

		return stale_nodes

	def remove_machines_from_layout(self, machines: list[MachineName]) -> list[str]:
		"""Remove the machines' nodes from the layout and apply it. Returns the node ids removed."""
		removed_nodes = []
		for node_id, machine in self.get_layout_nodes():
			if machine in machines:
				removed_nodes.append(node_id)

		if removed_nodes:
			removals = [{"id": node_id, "remove": True} for node_id in removed_nodes]
			self.assign_roles(removals)
			self.apply_staged_layout()

		return removed_nodes

	def is_draining(self) -> bool:
		"""Whether Garage is still moving data for an older layout version."""
		versions = self.layout_history().get("versions") or []
		for version in versions:
			if version.get("status") == "Draining":
				return True

		return False

	def get_nodes_moving_blocks(self) -> list[str]:
		"""Running nodes with blocks still queued to fetch or hand over, or blocks they failed
		to fetch. Garage leaves blocks out of the drain it reports."""
		answered = self.list_workers().get("success") or {}

		nodes = []
		for node_id in self.get_running_node_ids():
			# A node that did not answer cannot show its blocks have moved.
			if node_id not in answered:
				nodes.append(node_id)
				continue

			for worker in answered[node_id]:
				if not worker["name"].startswith("Block resync worker"):
					continue

				if worker.get("queueLength") or worker.get("persistentErrors"):
					nodes.append(node_id)
					break

		return nodes

	def get_layout_nodes(self) -> list[tuple[str, MachineName]]:
		"""Each node in the layout, applied or staged, with the machine name setup tagged it with."""
		layout = self.layout()
		roles = (layout.get("roles") or []) + (layout.get("stagedRoleChanges") or [])

		nodes = []
		for role in roles:
			# A staged removal carries no tags.
			if not role.get("tags"):
				continue

			node = (role["id"], role["tags"][0])
			if node not in nodes:
				nodes.append(node)

		return nodes

	def get_running_node_ids(self) -> list[str]:
		nodes = self.status().get("nodes") or []
		return [node["id"] for node in nodes if node.get("isUp")]

	def get_down_node_ids(self) -> list[str]:
		nodes = self.status().get("nodes") or []
		return [node["id"] for node in nodes if not node.get("isUp")]

	def skip_dead_nodes_left_out_of_layout(self) -> None:
		"""Stop older layout versions waiting for nodes that are down and no longer in the
		layout: they will not come back to hand over their data. Not while a node that keeps
		its role is down, since it may only be restarting and must still sync."""
		if not self.is_draining():
			return

		down_nodes = self.get_down_node_ids()
		if not down_nodes:
			return

		layout_nodes = [node_id for node_id, _ in self.get_layout_nodes()]
		for node_id in down_nodes:
			if node_id in layout_nodes:
				return

		self.skip_dead_nodes(self.layout_version())
