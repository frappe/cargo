from __future__ import annotations

import typing

import frappe
from frappe import _

from cargo.client_models import GATEWAY, STORAGE
from cargo.object_storage.doctype.object_storage_cluster.setup import Setup

if typing.TYPE_CHECKING:
	from frappe.integrations.doctype.webhook.webhook import Webhook

	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

SOURCE_HEADER = "X-FC-Source"
REGION_HEADER = "X-FC-Region"
SOURCE = "cargo"
REPORTED_STATUSES = ("Active", "Failed")
CLUSTER_SECRETS = ("rpc_secret", "admin_token", "metrics_token")


def can_add_gateway_node(cluster: ObjectStorageCluster) -> None:
	"""Whether this cluster can add a gateway node."""
	if cluster.gateway_node:
		frappe.throw(_("This cluster already has a gateway node."))


def can_add_storage_node(cluster: ObjectStorageCluster) -> None:
	"""Whether this cluster can add a storage node."""
	if cluster.status == "Setting Up":
		frappe.throw(_("Cannot add storage node to a cluster that is setting up."))


def can_trigger_setup(cluster: ObjectStorageCluster) -> None:
	"""Throw unless every machine is Running and storage nodes cover the replication factor."""
	ensure_no_other_cluster_serving_or_setting_up(cluster)
	ensure_no_running_workflow(cluster)

	if not cluster.gateway_node or cluster.gateway_node.status != "Running":
		frappe.throw(_("This cluster needs a running gateway node before it can be set up."))

	ensure_gateway_is_public(cluster)

	running_storage_nodes = [node for node in cluster.storage_nodes if node.status == "Running"]
	if len(running_storage_nodes) != len(cluster.storage_nodes):
		frappe.throw(
			_("Only {0} of {1} storage nodes are running. All must be running to set up.").format(
				len(running_storage_nodes), len(cluster.storage_nodes)
			)
		)

	if len(running_storage_nodes) < cluster.replication_factor:
		frappe.throw(
			_("This cluster has {0} storage nodes, but its replication factor needs {1}.").format(
				len(running_storage_nodes), cluster.replication_factor
			)
		)


def ensure_gateway_is_public(cluster: ObjectStorageCluster) -> None:
	"""Refuse setup before it changes anything, so a live gateway without one stays Active."""
	if not cluster.gateway_node.public_ipv4:
		frappe.throw(
			_("Gateway {0} has no public IPv4 address yet. Wait for Atlas to attach one.").format(
				cluster.gateway_node.name
			)
		)


def can_release_machines(cluster: ObjectStorageCluster, machines: list[str]) -> list[str]:
	"""Validate a release and return the machines Garage must drain first."""
	cluster.check_permission("write")

	if cluster.status == "Setting Up":
		frappe.throw(_("Cannot release machines from a cluster that is setting up."))

	ensure_no_running_workflow(cluster)
	ensure_machines_belong(cluster, machines)

	joined = get_joined_machines(cluster)
	joined_machines = [name for name in machines if name in joined]
	ensure_gateway_stays(cluster, machines, joined)
	ensure_enough_storage_nodes_stay(cluster, joined_machines, joined)

	if joined and cluster.garage_setup.is_draining():
		return list(machines)

	return joined_machines


def can_restore_metadata(cluster: ObjectStorageCluster, snapshot: str, machine: str) -> list[str]:
	"""Validate a restore of one machine. The whole cluster is never restored at once: it loses
	everything written since the snapshot."""
	frappe.only_for("System Manager")

	if not cluster.is_live:
		frappe.throw(_("Only a cluster that has served has metadata to restore."))

	ensure_no_running_workflow(cluster)
	ensure_machines_belong(cluster, [machine])
	ensure_peers_serve(cluster, machine)
	ensure_snapshot_is_backed_up(cluster, snapshot, [machine])
	return [machine]


def ensure_snapshot_is_backed_up(cluster: ObjectStorageCluster, snapshot: str, machines: list[str]) -> None:
	"""Every machine must have its own backup of the snapshot in the bucket."""
	garage = cluster.garage_setup
	if not garage.has_metadata_backup:
		frappe.throw(_("Set a Metadata Backup Endpoint and keys before restoring metadata."))

	missing = [name for name in machines if not garage.has_metadata_backup_of(garage.machine(name), snapshot)]
	if missing:
		frappe.throw(_("No backup of snapshot {0} for: {1}").format(snapshot, ", ".join(missing)))


def ensure_peers_serve(cluster: ObjectStorageCluster, machine: str) -> None:
	"""Require every peer to serve while one machine has its metadata restored."""
	if frappe.db.get_value("Machine", machine, "status") != "Running":
		frappe.throw(_("{0} is not running.").format(machine))

	garage = cluster.garage_setup
	if cluster.gateway_node and cluster.gateway_node.name == machine:
		witness = next((node for node in cluster.storage_nodes if node.status == "Running"), None)
		if not witness:
			frappe.throw(_("No running storage node can say whether the others serve."))
		garage = Setup(cluster, address=witness.address)

	joined = garage.healthy_nodes()
	down = [node.name for node in cluster.storage_nodes if node.name != machine and node.name not in joined]
	if down:
		frappe.throw(
			_("Restoring {0} needs every other storage node serving. Not serving: {1}").format(
				machine, ", ".join(down)
			)
		)


def ensure_machines_belong(cluster: ObjectStorageCluster, machines: list[str]) -> None:
	if not machines:
		frappe.throw(_("Name the machines to release."))

	cluster_machines = [row.machine for row in cluster.machines]
	unknown = [name for name in machines if name not in cluster_machines]
	if unknown:
		frappe.throw(_("{0} is not a machine of this cluster.").format(", ".join(unknown)))


def get_joined_machines(cluster: ObjectStorageCluster) -> set[str]:
	"""Return the machines Garage reports as healthy and joined."""
	if not cluster.gateway_node or not all(
		cluster.get_password(name, raise_exception=False) for name in CLUSTER_SECRETS
	):
		return set()

	joined = cluster.garage_setup.healthy_nodes()
	if cluster.is_live and not joined:
		frappe.throw(_("Unable to release nodes currently garage is blocked."))

	return joined


def ensure_gateway_stays(cluster: ObjectStorageCluster, machines: list[str], joined: set[str]) -> None:
	"""Keep the gateway after the cluster has served or the gateway has joined."""
	for row in cluster.machines:
		if row.role != GATEWAY or row.machine not in machines:
			continue

		if cluster.is_live or row.machine in joined:
			frappe.throw(_("Cannot release the gateway machine of this cluster."))


def ensure_enough_storage_nodes_stay(
	cluster: ObjectStorageCluster, joined_machines: list[str], joined: set[str]
) -> None:
	"""Keep enough joined storage nodes to satisfy the replication factor."""
	if not joined_machines:
		return

	remaining_storage_nodes = []
	for row in cluster.machines:
		if row.role == STORAGE and row.machine in joined and row.machine not in joined_machines:
			remaining_storage_nodes.append(row.machine)

	if len(remaining_storage_nodes) < cluster.replication_factor:
		frappe.throw(
			_(
				"Releasing would leave {0} storage nodes in the cluster, and the replication factor needs {1}."
			).format(len(remaining_storage_nodes), cluster.replication_factor)
		)


def get_running_workflow(cluster: ObjectStorageCluster) -> str | None:
	return frappe.db.exists(
		"Press Workflow",
		{
			"linked_doctype": cluster.doctype,
			"linked_docname": cluster.name,
			"status": ("in", ["Queued", "Running"]),
		},
	)


def ensure_no_running_workflow(cluster: ObjectStorageCluster) -> None:
	"""Keep layout changes out of an active setup or drain workflow."""
	if running_workflow := get_running_workflow(cluster):
		frappe.throw(_("Workflow {0} is still running on this cluster.").format(running_workflow))


def ensure_no_other_active_cluster(cluster: ObjectStorageCluster) -> None:
	active_cluster = frappe.db.exists(
		"Object Storage Cluster",
		{"status": "Active", "name": ("!=", cluster.name)},
	)
	if active_cluster:
		frappe.throw(_("Object Storage Cluster {0} is already Active.").format(active_cluster))


def ensure_no_other_cluster_serving_or_setting_up(cluster: ObjectStorageCluster) -> None:
	"""Setup points the regional DNS records at its gateway, so it needs every other cluster idle.
	The setup job checks again before it publishes, so two racing setups both fail instead."""
	other_cluster = frappe.db.get_value(
		"Object Storage Cluster",
		{"status": ("in", ("Active", "Setting Up")), "name": ("!=", cluster.name)},
		["name", "status"],
		as_dict=True,
	)
	if other_cluster:
		frappe.throw(_("Object Storage Cluster {0} is {1}.").format(other_cluster.name, other_cluster.status))


def webhook_name_for(cluster: str) -> str:
	"""Return the webhook name used for one storage cluster."""
	return f"object_storage_cluster-{cluster}"


def configure_storage_cluster_webhook(cluster: ObjectStorageCluster) -> None:
	"""Point a Frappe Webhook at Central for cluster status changes."""
	settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")
	if not settings.central_webhook_url:
		raise frappe.ValidationError(
			_("Central has not enrolled this Cargo yet, so there is nowhere to report to.")
		)

	secret = settings.get_password("central_webhook_secret", raise_exception=True)
	name = webhook_name_for(cluster.name)
	webhook: Webhook = (
		frappe.get_doc("Webhook", name) if frappe.db.exists("Webhook", name) else frappe.new_doc("Webhook")
	)
	webhook.name = name
	webhook.update(
		{
			"webhook_doctype": cluster.doctype,
			"webhook_docevent": "on_update",
			"request_url": settings.central_webhook_url,
			"request_method": "POST",
			"request_structure": "JSON",
			"condition": f"doc.status in {REPORTED_STATUSES}",
			"webhook_json": frappe.as_json(
				{
					"region": settings.region,
					"region_id": settings.region_id,
					"service": "storage",
					"status": "{{ 'Available' if doc.status == 'Active' else 'Not Available' }}",
					"service_endpoint": cluster.service_endpoint,
				}
			),
			"webhook_headers": [
				{"key": SOURCE_HEADER, "value": SOURCE},
				{"key": REGION_HEADER, "value": settings.region},
			],
			"enable_security": True,
			"webhook_secret": secret,
			"enabled": settings.central_webhook_enabled,
		}
	)
	webhook.save(ignore_permissions=True)
