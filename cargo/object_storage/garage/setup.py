from __future__ import annotations

import typing
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from typing import TypedDict

import frappe
from frappe import _
from frappe.utils.password import get_decrypted_password

from cargo.atlas_client import host_port
from cargo.client_models import GATEWAY, STORAGE
from cargo.object_storage.garage.client import Client, Error
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
		}

	def apply_staged_layout(self) -> dict:
		"""One version for everything staged. Garage refuses a layout that cannot hold a full
		copy, so a gateway and its storage nodes have to land together."""
		layout = self.layout()
		if not layout.get("stagedRoleChanges"):
			return layout

		return self.apply_layout(layout.get("version", 0) + 1)
