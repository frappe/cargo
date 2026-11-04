from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from typing import Any, TypedDict
from urllib.parse import urlsplit

import boto3
import frappe
from botocore.exceptions import ClientError
from frappe import _
from frappe.utils.password import get_decrypted_password

from cargo.atlas_client import AtlasClient, host_port
from cargo.auth import generate_machine_token
from cargo.client_models import GATEWAY, STORAGE
from cargo.object_storage.client import Client, Error
from cargo.ssh import SSH_TIMEOUT, run_over_ssh, script
from cargo.telemetry.vector import datum_refresh_script, install_script

BINARY_URL = "https://garagehq.deuxfleurs.fr/_releases/{version}/{arch}/garage"
CONF = ("object_storage", "conf", "garage")
GIGABYTE = 1000**3
# As many as Garage keeps on a node.
KEEP_METADATA_BACKUPS = 2
# How long curl may move one snapshot to or from the bucket. SSH gets a little more, for the
# MMDS key wait, and the whole stays under the long queue's job timeout.
BACKUP_TRANSFER_TIMEOUT = 1200
BACKUP_SSH_TIMEOUT = BACKUP_TRANSFER_TIMEOUT + 120
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
	vm_id: str


@dataclass(frozen=True)
class ConnectedNodes:
	"""One read of what Garage can see."""

	peers: list[NodeIdentifier]
	machines: set[MachineName]


class Setup(Client):
	"""Setting a cluster's machines up, and asking Garage what it sees."""

	@cached_property
	def machines(self) -> list[MachineRow]:
		"""Every Garage machine that has booted, gateway first: the rest reach the cluster through it."""
		machines = frappe.get_all(
			"Machine",
			filters={
				"reference_doctype": self.cluster.doctype,
				"reference_name": self.cluster.name,
				"status": "Running",
				"role": ("in", (GATEWAY, STORAGE)),
			},
			fields=["name", "role", "zone", "address", "disk_size_gb", "vm_id"],
			order_by="creation",
		)

		return sorted(machines, key=lambda machine: machine["role"] != GATEWAY)

	def key_for(self, machine: MachineRow) -> str:
		"""A machine is reached with the key it was built with. Password fields are not
		columns, so this cannot come off the machine query."""
		return get_decrypted_password("Machine", machine["name"], "ssh_private_key")

	def run(
		self,
		machine: MachineRow,
		script: str,
		on_output: Callable[[str], None] | None = None,
		timeout: int = SSH_TIMEOUT,
	) -> str:
		"""Every command a node is given, streamed to `on_output` as it arrives."""
		return run_over_ssh(
			machine["address"], script, self.key_for(machine), on_output=on_output, timeout=timeout
		)

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

	def install_metrics_exporter(
		self, machine: MachineRow, on_output: Callable[[str], None] | None = None
	) -> None:
		"""Install Vector and the timer that fetches the node's own Datum token. Safe to repeat.
		Vector scrapes Garage's metrics on the node itself, so they outlive Cargo."""
		garage_metrics = {
			"GARAGE_ADMIN_PORT": self.cluster.admin_port,
			"GARAGE_METRICS_TOKEN": self.secrets["metrics_token"],
			"GARAGE_SCRAPE_SECONDS": self.cluster.metrics_scrape_seconds,
		}
		self.replace_vm_metadata(machine)
		# Vector only installs once the node holds its own Datum token.
		self.run(machine, datum_refresh_script(), on_output)
		self.run(
			machine,
			install_script(
				machine["role"],
				machine["name"],
				self.cluster.region,
				cluster=self.cluster.name,
				environment=garage_metrics,
			),
			on_output,
		)

	def replace_vm_metadata(self, machine: MachineRow, extra: dict[str, str] | None = None) -> None:
		"""Atlas replaces the whole map, so the role and Datum token are always sent again."""
		metadata = {
			"role": machine["role"],
			"datum_telemetry_token": generate_machine_token(machine["role"], machine["name"]),
			**(extra or {}),
		}
		AtlasClient.from_settings().replace_vm_metadata(machine["vm_id"], metadata)

	@property
	def has_metadata_backup(self) -> bool:
		cluster = self.cluster
		return bool(cluster.metadata_backup_endpoint and cluster.metadata_bucket_access_key)

	def get_backup_environment(self, machine: MachineRow) -> dict[str, str]:
		"""Where this machine's own backups live. Keys reach the node through MMDS, never the
		SSH script."""
		cluster = self.cluster
		self.replace_vm_metadata(
			machine,
			{
				"metadata_bucket_access_key": cluster.metadata_bucket_access_key,
				"metadata_bucket_secret_key": cluster.get_password("metadata_bucket_secret_key"),
			},
		)
		return {
			"TRANSFER_TIMEOUT": BACKUP_TRANSFER_TIMEOUT,
			"BACKUP_ENDPOINT": cluster.metadata_backup_endpoint,
			"BACKUP_REGION": cluster.region,
			"BACKUP_PREFIX": self.get_backup_prefix(machine),
		}

	def get_backup_prefix(self, machine: MachineRow) -> str:
		return f"{self.cluster.name}/{machine['name']}"

	@cached_property
	def backup_bucket(self) -> str:
		return urlsplit(self.cluster.metadata_backup_endpoint).path.strip("/")

	@cached_property
	def backup_client(self) -> Any:
		cluster = self.cluster
		endpoint = urlsplit(cluster.metadata_backup_endpoint)
		return boto3.client(
			"s3",
			endpoint_url=f"{endpoint.scheme}://{endpoint.netloc}",
			region_name=cluster.region,
			aws_access_key_id=cluster.metadata_bucket_access_key,
			aws_secret_access_key=cluster.get_password("metadata_bucket_secret_key"),
		)

	def has_metadata_backup_of(self, machine: MachineRow, snapshot: str) -> bool:
		"""Whether the bucket holds this machine's own backup of the snapshot."""
		key = f"{self.get_backup_prefix(machine)}/{snapshot}/db.lmdb"
		try:
			self.backup_client.head_object(Bucket=self.backup_bucket, Key=key)
		except ClientError as error:
			if error.response["Error"]["Code"] in ("404", "NoSuchKey"):
				return False
			raise

		return True

	def prune_metadata_backups(self, machine: MachineRow) -> list[str]:
		"""Delete all but this machine's newest backups. Names sort in time order. Returns the
		keys deleted."""
		pages = self.backup_client.get_paginator("list_objects_v2").paginate(
			Bucket=self.backup_bucket, Prefix=f"{self.get_backup_prefix(machine)}/"
		)
		keys = sorted(item["Key"] for page in pages for item in page.get("Contents", []))
		stale = keys[:-KEEP_METADATA_BACKUPS]
		for key in stale:
			self.backup_client.delete_object(Bucket=self.backup_bucket, Key=key)

		return stale

	def upload_metadata_snapshots(
		self, machine: MachineRow, name: str, on_output: Callable[[str], None] | None = None
	) -> str:
		"""Ship the node's newest snapshot to the bucket as `name`, then delete the local copies."""
		environment = {
			"METADATA_DIR": self.cluster.metadata_dir,
			"NAME": name,
			**self.get_backup_environment(machine),
		}
		return self.run(
			machine,
			script(*CONF, "upload_metadata_snapshots.sh", environment=environment),
			on_output,
			timeout=BACKUP_SSH_TIMEOUT,
		)

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

	def grow_filesystem(self, machine: MachineRow, on_output: Callable[[str], None] | None = None) -> None:
		"""Fill the disk Atlas grew. Until then Garage still sees the old size."""
		environment = {"DATA_DIR": self.cluster.data_dir}
		self.run(machine, script(*CONF, "grow_filesystem.sh", environment=environment), on_output)

	def stop_garage(self, machine: MachineRow, on_output: Callable[[str], None] | None = None) -> str:
		return self.run(machine, "systemctl stop garage", on_output)

	def restore_metadata(
		self, machine: MachineRow, snapshot: str, on_output: Callable[[str], None] | None = None
	) -> str:
		"""Swap a snapshot in for the node's database. The live one is kept beside it."""
		environment = {
			"GARAGE_BINARY": self.cluster.garage_binary,
			"METADATA_DIR": self.cluster.metadata_dir,
			"SNAPSHOT": snapshot,
		}
		return self.run(machine, script(*CONF, "restore_metadata.sh", environment=environment), on_output)

	def fetch_metadata_snapshot(
		self, machine: MachineRow, snapshot: str, on_output: Callable[[str], None] | None = None
	) -> str:
		"""Download the node's own backup of a snapshot, unless it still holds it."""
		environment = {
			"METADATA_DIR": self.cluster.metadata_dir,
			"SNAPSHOT": snapshot,
			**self.get_backup_environment(machine),
		}
		return self.run(
			machine,
			script(*CONF, "fetch_metadata_snapshot.sh", environment=environment),
			on_output,
			timeout=BACKUP_SSH_TIMEOUT,
		)

	def repair_tables(self, machine: MachineRow, on_output: Callable[[str], None] | None = None) -> str:
		"""Have the node fetch every table entry its peers hold newer than its own. Garage runs
		the repair in the background, so this returns once it has started."""
		return self.run(machine, f"{shlex.quote(self.cluster.garage_binary)} repair --yes tables", on_output)

	def stage_storage_capacities(self) -> list[str]:
		"""Stage each storage node's capacity from its machine's disk, where they differ. Returns
		the node ids staged."""
		disks = {
			machine["name"]: machine["disk_size_gb"] * GIGABYTE
			for machine in self.machines
			if machine["role"] == STORAGE
		}

		roles = []
		for role in self.layout().get("roles") or []:
			machine = (role.get("tags") or [None])[0]
			if machine in disks and role.get("capacity") != disks[machine]:
				roles.append(
					{"id": role["id"], "zone": role["zone"], "tags": role["tags"], "capacity": disks[machine]}
				)

		if roles:
			self.assign_roles(roles)

		return [role["id"] for role in roles]

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
