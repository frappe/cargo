from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import typing
from functools import cached_property
from typing import TypedDict

import frappe
from frappe import _

from cargo.object_storage.client_models import STORAGE
from cargo.object_storage.credentials import REQUIRED_CREDENTIALS

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

CONFIG_TEMPLATE = "object_storage/conf/garage_node_config.jinja2"
UNIT_TEMPLATE = "object_storage/conf/garage_service.jinja2"
INSTALL_TEMPLATE = "object_storage/conf/install.jinja2"

BINARY_URL = "https://garagehq.deuxfleurs.fr/_releases/{version}/{arch}/garage"
SSH_TIMEOUT = 600

#: `Machine.name`, e.g. ``OSC-eu-1-0001-storage-0001``.
MachineName = str

#: What Garage calls a node identifier: ``<node id>@<address>:<rpc port>``, exactly as
#: ``garage node id`` prints it. Goes straight into `bootstrap_peers`.
NodeIdentifier = str


class MachineRow(TypedDict):
	"""The `Machine` fields the installer reads."""

	name: MachineName
	vm_id: str
	role: str
	zone: str
	ipv4_address: str


class InstallError(RuntimeError):
	"""A node failed to install or start."""


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

	def is_installed(self) -> bool:
		"""True when every machine already runs the wanted Garage version."""
		wanted = self.cluster.garage_version
		for machine in self.machines:
			try:
				installed = run_over_ssh(
					machine["ipv4_address"], f"{self.cluster.garage_binary} --version", self.ssh_key
				)
			except InstallError:
				return False
			if wanted not in installed:
				return False

		return True

	def is_laid_out(self) -> bool:
		"""True when the cluster already has an applied layout."""
		try:
			shown = run_over_ssh(self.machines[0]["ipv4_address"], "garage layout show", self.ssh_key)
		except InstallError:
			return False

		return "No nodes currently have a role" not in shown

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

	def assign_layout(self, identifiers: dict[MachineName, NodeIdentifier]) -> str:
		"""Give every node its role and apply as one version."""
		commands = []
		for machine in self.machines:
			node_id = identifiers[machine["name"]].split("@")[0]
			shape = f"-c {self.cluster.disk_gb}GB" if machine["role"] == STORAGE else "-g"
			commands.append(
				f"garage layout assign {shape} -z {machine['zone']} -t {machine['name']} {node_id}"
			)

		commands.append("garage layout apply --version 1")

		return run_over_ssh(self.machines[0]["ipv4_address"], "\n".join(commands), self.ssh_key)

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


def run_over_ssh(address: str, script: str, key: str | None, user: str = "root") -> str:
	"""Pipe a script to ``bash -s`` and return its stdout."""
	if not key:
		frappe.throw(_("This cluster has no SSH private key; its machines cannot be reached."))

	with tempfile.NamedTemporaryFile("w", delete=False) as key_file:
		key_file.write(key if key.endswith("\n") else f"{key}\n")
		path = key_file.name
	os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

	try:
		result = subprocess.run(
			[
				"ssh",
				"-i",
				path,
				"-o",
				"IdentitiesOnly=yes",
				"-o",
				"StrictHostKeyChecking=accept-new",
				"-o",
				"UserKnownHostsFile=/dev/null",
				"-o",
				"ConnectTimeout=15",
				f"{user}@{address}",
				"bash -s",
			],
			input=script,
			capture_output=True,
			text=True,
			timeout=SSH_TIMEOUT,
			check=False,
		)
	finally:
		os.unlink(path)

	if result.returncode != 0:
		raise InstallError(f"{address}: {(result.stderr or result.stdout).strip()[:500]}")

	return result.stdout
