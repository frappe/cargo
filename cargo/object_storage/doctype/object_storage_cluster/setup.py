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
from cargo.object_storage.client_models import STORAGE
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
#: `Machine.name`, e.g. ``OSC-eu-1-0001-storage-0001``.
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


class MetadataBucketInfo(TypedDict):
	"""The metadata bucket and its credentials."""

	name: str
	access_key: str
	secret_key: str


class SetupError(SshError):
	"""A node failed to set up."""


class ClusterSetup:
	"""Turns a cluster's machines into running Garage nodes."""

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
		return frappe.get_all(
			"Machine",
			filters={"reference_doctype": self.cluster.doctype, "reference_name": self.cluster.name},
			fields=["name", "vm_id", "role", "zone", "ipv4_address"],
			order_by="creation",
		)

	@cached_property
	def ssh_key(self) -> str:
		return self.cluster.get_password("ssh_private_key")

	@cached_property
	def admin(self) -> GarageAdminClient:
		return GarageAdminClient.for_cluster(self.cluster)

	def run(self, address: str, script: str, quiet: bool = False) -> str:
		"""Every command the setup runs, streamed into the log unless it prints a secret."""
		return run_over_ssh(address, script, self.ssh_key, on_output=None if quiet else self.on_output)

	def layout_version(self) -> int:
		"""The applied layout version, zero if none. Staged changes are a separate field."""
		try:
			return self.admin.layout().get("version", 0)
		except GarageError:
			return 0

	def healthy_nodes(self) -> set[str]:
		"""The machine names Garage reports as up, read from the node tags setup assigned."""
		try:
			nodes = self.admin.status().get("nodes") or []
		except GarageError:
			return set()

		tags = {tag for node in nodes if node.get("isUp") for tag in (node.get("role") or {}).get("tags", [])}

		return {machine["name"] for machine in self.machines if machine["name"] in tags}

	def node_identifiers(self) -> dict[MachineName, NodeIdentifier]:
		"""Only answers once a node has started, since Garage keys itself on first launch."""
		return {
			machine["name"]: self.run(machine["ipv4_address"], "garage node id -q").strip().splitlines()[-1]
			for machine in self.machines
		}

	def install_script(self, machine: MachineRow, peers: list[NodeIdentifier]) -> str:
		"""This kind's script, with the cluster's settings exported ahead of it."""
		exports = "\n".join(
			f"export {key}={shlex.quote(str(value))}"
			for key, value in self.install_environment(machine, peers).items()
		)
		script = Path(
			frappe.get_app_path("cargo", "object_storage", "conf", "garage", "install.sh")
		).read_text()

		return f"{exports}\n{script}"

	def install_environment(self, machine: MachineRow, peers: list[NodeIdentifier]) -> dict[str, str]:
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
			"BOOTSTRAP_PEERS": " ".join(peers),
			"RPC_SECRET": self.secrets["rpc_secret"],
			"ADMIN_TOKEN": self.secrets["admin_token"],
			"METRICS_TOKEN": self.secrets["metrics_token"],
		}

	def bootstrap_machine(self, machine: MachineRow, peers: list[NodeIdentifier]) -> str:
		if self.on_output:
			self.on_output(f"\n=== {machine['name']} ({machine['ipv4_address']}) ===\n")

		return self.run(machine["ipv4_address"], self.install_script(machine, peers))

	def bootstrap_machines(self) -> dict[MachineName, NodeIdentifier]:
		"""Bring the cluster up, twice: peers are unknown until the nodes have run once."""
		if not self.machines:
			frappe.throw(_("This cluster has no machines."))

		for machine in self.machines:
			self.bootstrap_machine(machine, peers=[])

		identifiers = self.node_identifiers()
		for machine in self.machines:
			self.bootstrap_machine(machine, list(identifiers.values()))

		return identifiers

	def assign_layout(self, identifiers: dict[MachineName, NodeIdentifier]) -> dict:
		"""Stage every node's role, then apply the lot as one version."""
		roles = []
		for machine in self.machines:
			role = {
				"id": identifiers[machine["name"]].split("@")[0],
				"zone": machine["zone"],
				"tags": [machine["name"]],
			}
			if machine["role"] == STORAGE:
				role["capacity"] = self.cluster.disk_gb * GIGABYTE

			roles.append(role)

		self.admin.assign_roles(roles)

		return self.admin.apply_layout(self.layout_version() + 1)

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
