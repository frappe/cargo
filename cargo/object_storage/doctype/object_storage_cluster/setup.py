from __future__ import annotations

import re
import shlex
import typing
from collections.abc import Callable
from functools import cached_property
from pathlib import Path
from typing import TypedDict

import frappe
from frappe import _

from cargo.garage_admin_client import GarageAdminClient, GarageError
from cargo.object_storage.client_models import GATEWAY, STORAGE
from cargo.object_storage.credentials import REQUIRED_CREDENTIALS
from cargo.ssh import SshError, run_over_ssh

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

BINARY_URL = "https://garagehq.deuxfleurs.fr/_releases/{version}/{arch}/garage"
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
	vm_id: str
	role: str
	zone: str
	ipv4_address: str
	disk_size_gb: int


class MetadataBucketInfo(TypedDict):
	"""The metadata bucket and its credentials."""

	name: str
	access_key: str
	secret_key: str


class SetupError(SshError):
	"""A node failed to set up."""


class ClusterSetup:
	"""Turns a cluster's machines into running Garage nodes, one machine at a time."""

	def __init__(self, cluster: ObjectStorageCluster, on_output: Callable[[str], None] | None = None):
		self.cluster = cluster
		self.on_output = on_output
		self.secrets = {
			name: cluster.get_password(name, raise_exception=False) for name in REQUIRED_CREDENTIALS
		}
		missing = [name for name, value in self.secrets.items() if not value]
		if missing:
			frappe.throw(_(f"This cluster has no {', '.join(missing)}. Mint its credentials first."))

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
			fields=["name", "vm_id", "role", "zone", "ipv4_address", "disk_size_gb"],
			order_by="creation",
		)

		return sorted(machines, key=lambda machine: machine["role"] != GATEWAY)

	@cached_property
	def ssh_key(self) -> str:
		return self.cluster.get_password("ssh_private_key")

	@cached_property
	def admin(self) -> GarageAdminClient:
		return GarageAdminClient.for_cluster(self.cluster)

	def run(self, address: str, script: str) -> str:
		"""Every command the setup runs, streamed into the log."""
		return run_over_ssh(address, script, self.ssh_key, on_output=self.on_output)

	def layout_version(self) -> int:
		"""The applied layout version, zero if none. Staged changes are a separate field."""
		try:
			return self.admin.layout().get("version", 0)
		except GarageError:
			return 0

	def healthy_nodes(self) -> set[MachineRow]:
		"""The machine names Garage reports as up, read from the node tags setup assigned."""
		try:
			nodes = self.admin.status().get("nodes") or []
		except GarageError:
			return set()

		tags = {tag for node in nodes if node.get("isUp") for tag in (node.get("role") or {}).get("tags", [])}

		return {machine for machine in self.machines if machine["name"] in tags}

	def peers(self) -> list[NodeIdentifier]:
		"""The nodes Garage can reach, as it addresses them itself."""
		try:
			nodes = self.admin.status().get("nodes") or []
		except GarageError:
			return []

		return [f"{node['id']}@{node['addr']}" for node in nodes if node.get("isUp") and node.get("addr")]

	def unjoined_machines(self) -> list[MachineRow]:
		"""Machines that have booted but are no part of the cluster yet."""
		joined = self.healthy_nodes()

		return [machine for machine in self.machines if machine["name"] not in joined]

	def machine(self, name: MachineName) -> MachineRow:
		"""One booted machine of this cluster, by name."""
		machine = next((row for row in self.machines if row["name"] == name), None)
		if not machine:
			frappe.throw(_(f"{name} is not a machine of this cluster, or has not booted."))

		return machine

	def node_identifier(self, machine: MachineRow) -> NodeIdentifier:
		"""Only answers once the node has started, since Garage keys itself on first launch."""
		return self.run(machine["ipv4_address"], "garage node id -q").strip().splitlines()[-1]

	def script(self, name: str, environment: dict[str, str]) -> str:
		"""One of this service's scripts, with its arguments exported ahead of it."""
		exports = "\n".join(f"export {key}={shlex.quote(str(value))}" for key, value in environment.items())
		body = Path(frappe.get_app_path("cargo", "object_storage", "conf", "garage", name)).read_text()

		return f"{exports}\n{body}"

	def install_environment(self, machine: MachineRow) -> dict[str, str]:
		"""What a node needs to write its own garage.toml and unit."""
		cluster = self.cluster

		return {
			"GARAGE_BINARY": cluster.garage_binary,
			"GARAGE_VERSION": cluster.garage_version,
			"BINARY_URL": BINARY_URL.format(version=cluster.garage_version, arch=cluster.garage_arch),
			"METADATA_DIR": cluster.metadata_dir,
			"DATA_DIR": cluster.data_dir,
			"RPC_PUBLIC_ADDR": machine["ipv4_address"],
			"REGION": cluster.region,
			"BASE_DOMAIN": cluster.base_domain,
			"REPLICATION_FACTOR": cluster.replication_factor,
			"RPC_PORT": cluster.rpc_port,
			"S3_PORT": cluster.s3_port,
			"WEB_PORT": cluster.web_port,
			"K2V_PORT": cluster.k2v_port,
			"ADMIN_PORT": cluster.admin_port,
			"RPC_SECRET": self.secrets["rpc_secret"],
			"ADMIN_TOKEN": self.secrets["admin_token"],
			"METRICS_TOKEN": self.secrets["metrics_token"],
		}

	def install(self, machine: MachineRow) -> str:
		return self.run(machine["ipv4_address"], self.script("install.sh", self.install_environment(machine)))

	def record_peers(self, machine: MachineRow, peers: list[NodeIdentifier]) -> str:
		"""Where a node looks for the others after a reboot. Nothing restarts to read it."""
		return self.run(
			machine["ipv4_address"], self.script("set_peers.sh", {"BOOTSTRAP_PEERS": " ".join(peers)})
		)

	def setup_machine(self, machine: MachineRow) -> None:
		"""Install Garage on one machine and fold it into whatever cluster already exists."""
		if self.on_output:
			self.on_output(f"\n=== {machine['name']} ({machine['ipv4_address']}) ===\n")

		self.install(machine)
		identifier = self.node_identifier(machine)
		if self.healthy_nodes():
			# The gateway answers for the cluster, so this is the cluster reaching for the
			# new node rather than the other way round.
			self.admin.connect_nodes([identifier])

		self.stage_role(machine, identifier)
		# Only this machine is told: the nodes already running know each other, and one live
		# peer is all a node needs to find the rest after a reboot.
		self.record_peers(machine, self.peers() or [identifier])

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
