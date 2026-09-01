from __future__ import annotations

import re
import typing
from functools import cached_property
from typing import TypedDict

import frappe
from frappe import _

from cargo.object_storage.client_models import STORAGE
from cargo.object_storage.credentials import REQUIRED_CREDENTIALS
from cargo.ssh import SshError, run_over_ssh

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

CONFIG_TEMPLATE = "object_storage/conf/garage_node_config.jinja2"
UNIT_TEMPLATE = "object_storage/conf/garage_service.jinja2"
INSTALL_TEMPLATE = "object_storage/conf/install.jinja2"

BINARY_URL = "https://garagehq.deuxfleurs.fr/_releases/{version}/{arch}/garage"


#: `Machine.name`, e.g. ``OSC-eu-1-0001-storage-0001``.
MachineName = str

#: What Garage calls a node identifier: ``<node id>@<address>:<rpc port>``, exactly as
#: ``garage node id`` prints it. Goes straight into `bootstrap_peers`.
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

	def __init__(self, cluster: ObjectStorageCluster):
		self.cluster = cluster
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

	def layout_version(self) -> int:
		"""The applied layout version. Zero means nothing has been applied yet.

		Read from the version line, not from the roles: `garage layout show` also prints
		staged changes, so a staged-but-unapplied layout would otherwise look finished.
		"""
		try:
			shown = run_over_ssh(self.machines[0]["ipv4_address"], "garage layout show", self.ssh_key)
		except SetupError:
			return 0

		found = re.search(r"Current cluster layout version:\s*(\d+)", shown)

		return int(found.group(1)) if found else 0

	def healthy_nodes(self) -> set[str]:
		"""The machine names Garage reports as healthy, read from the node tags setup
		assigned."""
		try:
			shown = run_over_ssh(self.machines[0]["ipv4_address"], "garage status", self.ssh_key)
		except SetupError:
			return set()

		section = shown.split("FAILED NODES")[0]

		return {machine["name"] for machine in self.machines if f"[{machine['name']}]" in section}

	def node_identifiers(self) -> dict[MachineName, NodeIdentifier]:
		"""Only answers once a node has started, since Garage generates its key on first
		launch."""
		return {
			machine["name"]: run_over_ssh(machine["ipv4_address"], "garage node id -q", self.ssh_key).strip()
			for machine in self.machines
		}

	def install_script(self, machine: MachineRow, peers: list[NodeIdentifier]) -> str:
		return frappe.render_template(
			INSTALL_TEMPLATE,
			{
				"cluster": self.cluster,
				"binary_url": BINARY_URL.format(
					version=self.cluster.garage_version, arch=self.cluster.garage_arch
				),
				"node_config": self.node_config(machine, peers),
				"systemd_unit": self.systemd_unit(),
			},
			is_path=True,
		)

	def node_config(self, machine: MachineRow, peers: list[NodeIdentifier]) -> str:
		return frappe.render_template(
			CONFIG_TEMPLATE,
			{
				"cluster": self.cluster,
				"rpc_public_addr": machine["ipv4_address"],
				"bootstrap_peers": peers,
				**self.secrets,
			},
			is_path=True,
		)

	def systemd_unit(self) -> str:
		return frappe.render_template(
			UNIT_TEMPLATE, {"garage_binary": self.cluster.garage_binary}, is_path=True
		)

	def bootstrap_machine(self, machine: MachineRow, peers: list[NodeIdentifier]) -> str:
		return run_over_ssh(machine["ipv4_address"], self.install_script(machine, peers), self.ssh_key)

	def bootstrap_machines(self) -> dict[MachineName, NodeIdentifier]:
		"""Bring the cluster up.

		Twice: peers are unknown until the nodes have run once.
		"""
		if not self.machines:
			frappe.throw(_("This cluster has no machines."))

		for machine in self.machines:
			self.bootstrap_machine(machine, peers=[])

		identifiers = self.node_identifiers()
		for machine in self.machines:
			self.bootstrap_machine(machine, list(identifiers.values()))

		return identifiers

	def assign_layout(self, identifiers: dict[MachineName, NodeIdentifier]) -> str:
		"""Give every node its role and apply as one version, run on a single machine and gossips around."""
		commands = []
		for machine in self.machines:
			node_id = identifiers[machine["name"]].split("@")[0]
			shape = f"-c {self.cluster.disk_gb}GB" if machine["role"] == STORAGE else "-g"
			commands.append(
				f"garage layout assign {node_id} {shape} -z {machine['zone']} -t {machine['name']}"
			)

		commands.append(f"garage layout apply --version {self.layout_version() + 1}")

		return run_over_ssh(self.machines[0]["ipv4_address"], "\n".join(commands), self.ssh_key)

	def create_metadata_bucket(self) -> MetadataBucketInfo:
		"""Create the metadata bucket on one node cluster-wide."""
		metadata_bucket = f"{self.cluster.name}-metadata"
		metadata_bucket_key = f"{metadata_bucket}-key"
		run_over_ssh(
			self.machines[0]["ipv4_address"],
			f"garage bucket create {metadata_bucket} && ",
			self.ssh_key,
		)
		key_create_output = run_over_ssh(
			self.machines[0]["ipv4_address"],
			f"garage bucket key create {metadata_bucket} {metadata_bucket_key}",
			self.ssh_key,
		)
		# Find access and secret key from the output
		access_key_match = re.search(r"Access Key:\s*(\S+)", key_create_output)
		secret_key_match = re.search(r"Secret Key:\s*(\S+)", key_create_output)
		if access_key_match and secret_key_match:
			run_over_ssh(
				self.machines[0]["ipv4_address"],
				f"garage bucket allow --read --write {metadata_bucket} --key {metadata_bucket_key}",
			)
			return MetadataBucketInfo(
				name=metadata_bucket,
				access_key=access_key_match.group(1),
				secret_key=secret_key_match.group(1),
			)

		raise RuntimeError("Failed to create metadata bucket or retrieve keys.")
