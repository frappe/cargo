from __future__ import annotations

import re
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
from cargo.garage_admin_client import GarageAdminClient, GarageError
from cargo.object_storage.metadata_bucket import MetadataBucketInfo
from cargo.ssh import SshError, run_over_ssh, script

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

BINARY_URL = "https://garagehq.deuxfleurs.fr/_releases/{version}/{arch}/garage"
CONF = ("object_storage", "conf", "garage")
#: Lowercase alphanumerics, dots and hyphens, 3-63 characters, alphanumeric at both ends.
BUCKET_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
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


class SetupError(SshError):
	"""A node failed to set up."""


class Garage:
	"""One cluster's Garage: setting its machines up, and asking it what it sees."""

	def __init__(self, cluster: ObjectStorageCluster):
		self.cluster = cluster

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

	@cached_property
	def admin(self) -> GarageAdminClient:
		return GarageAdminClient.for_cluster(self.cluster)

	def run(self, machine: MachineRow, script: str, on_output: Callable[[str], None] | None = None) -> str:
		"""Every command a node is given, streamed to `on_output` as it arrives."""
		return run_over_ssh(machine["address"], script, self.key_for(machine), on_output=on_output)

	def layout_version(self) -> int:
		"""The applied layout version, zero if none. Staged changes are a separate field."""
		try:
			return self.admin.layout().get("version", 0)
		except GarageError:
			return 0

	def get_connected_nodes(self) -> ConnectedNodes:
		"""The nodes Garage can reach, as it addresses them, and whose machines they are."""
		try:
			nodes = self.admin.status().get("nodes") or []
			# A node is joined once it is up and tagged, whether or not the layout carrying
			# that tag has been applied: applying is a separate step.
			staged = self.admin.layout().get("stagedRoleChanges") or []
		except GarageError:
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

	def install_environment(self, machine: MachineRow) -> dict[str, str]:
		"""What a node needs to write its own garage.toml and unit."""
		cluster = self.cluster

		return {
			"GARAGE_BINARY": cluster.garage_binary,
			"GARAGE_VERSION": cluster.garage_version,
			"BINARY_URL": BINARY_URL.format(version=cluster.garage_version, arch=cluster.garage_arch),
			"METADATA_DIR": cluster.metadata_dir,
			"DATA_DIR": cluster.data_dir,
			"RPC_PUBLIC_ADDR": host_port(machine["address"], cluster.rpc_port),
			"REGION": cluster.region,
			"BASE_DOMAIN": cluster.base_domain,
			"REPLICATION_FACTOR": cluster.replication_factor,
			"RPC_PORT": cluster.rpc_port,
			"S3_PORT": cluster.s3_port,
			"WEB_PORT": cluster.web_port,
			"K2V_PORT": cluster.k2v_port,
			"ADMIN_PORT": cluster.admin_port,
			"RPC_SECRET": self.cluster.get_password("rpc_secret"),
			"ADMIN_TOKEN": self.cluster.get_password("admin_token"),
			"METRICS_TOKEN": self.cluster.get_password("metrics_token"),
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
		self.admin.connect_nodes([identifier])
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

		return self.admin.assign_roles([role])

	def apply_layout(self) -> dict:
		"""One version for everything staged. Garage refuses a layout that cannot hold a full
		copy, so a gateway and its storage nodes have to land together."""
		layout = self.admin.layout()
		if not layout.get("stagedRoleChanges"):
			return layout

		return self.admin.apply_layout(layout.get("version", 0) + 1)

	def create_metadata_bucket(self) -> MetadataBucketInfo:
		"""Create the metadata bucket and a key that can read and write it, idempotently."""
		bucket_name = f"{self.cluster.name}-metadata".casefold()
		if not BUCKET_NAME.fullmatch(bucket_name):
			frappe.throw(_(f"{bucket_name} is not a legal S3 bucket name."))

		key_name = f"{bucket_name}-key"

		bucket = self.admin.bucket(bucket_name) or self.admin.create_bucket(bucket_name)
		key = self.admin.key(key_name) or self.admin.create_key(key_name)
		if not key.get("secretAccessKey"):
			raise SetupError(f"Garage returned no secret for {key_name}.")

		self.admin.allow_bucket_key(bucket["id"], key["accessKeyId"])

		return MetadataBucketInfo(
			name=bucket_name, access_key=key["accessKeyId"], secret_key=key["secretAccessKey"]
		)
