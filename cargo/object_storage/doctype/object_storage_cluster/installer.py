from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import typing

import frappe
from frappe import _

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


class InstallError(RuntimeError):
	"""A node failed to install or start."""


class ClusterSetup:
	"""Turns a cluster's machines into running Garage nodes.

	Two passes, because a node's id is generated at first start: install every node with
	no peers, collect the ids they report, then reinstall with `bootstrap_peers` filled so
	they find each other.
	"""

	def __init__(self, cluster: ObjectStorageCluster):
		self.cluster = cluster
		self.secrets = {
			name: cluster.get_password(name, raise_exception=False) for name in REQUIRED_CREDENTIALS
		}
		missing = [name for name, value in self.secrets.items() if not value]
		if missing:
			frappe.throw(_(f"This cluster has no {', '.join(missing)}. Mint its credentials first."))

	def machines(self) -> list[dict]:
		return frappe.get_all(
			"Machine",
			filters={"reference_doctype": self.cluster.doctype, "reference_name": self.cluster.name},
			fields=["name", "vm_id", "role", "ipv4_address"],
			order_by="creation",
		)

	def node_config(self, machine: dict, peers: list[str]) -> str:
		return frappe.render_template(
			CONFIG_TEMPLATE,
			{
				"metadata_dir": self.cluster.metadata_dir,
				"data_dir": self.cluster.data_dir,
				"replication_factor": self.cluster.replication_factor,
				"rpc_bind_addr": "[::]",
				"rpc_public_addr": machine["ipv4_address"],
				"rpc_port": self.cluster.rpc_port,
				"api_bind_addr": "[::]",
				"s3_port": self.cluster.s3_port,
				"web_bind_addr": "[::]",
				"web_port": self.cluster.web_port,
				"admin_port": self.cluster.admin_port,
				"k2v_port": self.cluster.k2v_port,
				"region": self.cluster.region,
				"base_domain": self.cluster.base_domain,
				"bootstrap_peers": peers,
				**self.secrets,
			},
			is_path=True,
		)

	def systemd_unit(self) -> str:
		return frappe.render_template(
			UNIT_TEMPLATE, {"garage_binary": self.cluster.garage_binary}, is_path=True
		)

	def install_script(self, machine: dict, peers: list[str]) -> str:
		return frappe.render_template(
			INSTALL_TEMPLATE,
			{
				"metadata_dir": self.cluster.metadata_dir,
				"data_dir": self.cluster.data_dir,
				"garage_binary": self.cluster.garage_binary,
				"garage_version": self.cluster.garage_version,
				"binary_url": BINARY_URL.format(
					version=self.cluster.garage_version, arch=self.cluster.garage_arch
				),
				"node_config": self.node_config(machine, peers),
				"systemd_unit": self.systemd_unit(),
			},
			is_path=True,
		)

	def install(self, machine: dict, peers: list[str]) -> str:
		"""Run the install script on one machine. Returns the node id it reports."""
		result = run_over_ssh(
			machine["ipv4_address"],
			self.install_script(machine, peers),
			self.cluster.get_password("ssh_private_key", raise_exception=False),
		)
		node_id = result.strip().splitlines()[-1].strip() if result.strip() else ""
		if not node_id:
			raise InstallError(f"{machine['vm_id']} reported no node id")

		return node_id

	def run(self) -> dict[str, str]:
		"""Install every node, then reinstall with peers so the cluster forms."""
		machines = self.machines()
		if not machines:
			frappe.throw(_("This cluster has no machines."))

		node_ids = {machine["vm_id"]: self.install(machine, peers=[]) for machine in machines}
		peers = [
			f"{node_id}@{_address(machines, vm_id)}:{self.cluster.rpc_port}"
			for vm_id, node_id in node_ids.items()
		]
		for machine in machines:
			self.install(machine, peers)

		return node_ids


def _address(machines: list[dict], vm_id: str) -> str:
	return next(machine["ipv4_address"] for machine in machines if machine["vm_id"] == vm_id)


def run_over_ssh(address: str, script: str, key: str | None, user: str = "root") -> str:
	"""Pipe a script to ``bash -s`` on a machine and return its stdout."""
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
