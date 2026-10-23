from __future__ import annotations

import typing
from datetime import datetime, timedelta

import frappe
from frappe.utils import get_datetime, now_datetime

from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import get_running_workflow
from cargo.object_storage.health import live_clusters

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)


def take_metadata_snapshots() -> None:
	"""Snapshot every live cluster's metadata once Cargo Settings says one is due. Scheduled in
	`hooks.py`."""
	interval_hours = frappe.db.get_single_value("Cargo Settings", "metadata_snapshot_interval_hours")
	if not interval_hours:
		return

	cluster_name = frappe.db.get_value("Object Storage Cluster", {"status": "Active"})

	if not cluster_name:
		return

	cluster: ObjectStorageCluster = frappe.get_doc("Object Storage Cluster", cluster_name)
	if not is_snapshot_due(cluster.last_metadata_snapshot_at, interval_hours, now_datetime()):
		return

	if get_running_workflow(cluster):
		return

	try:
		take_metadata_snapshot(cluster)
	except Exception:
		frappe.log_error(title=f"Could not snapshot {cluster_name} metadata")


def is_snapshot_due(last: datetime | str | None, interval_hours: float, now: datetime) -> bool:
	return not last or now >= get_datetime(last) + timedelta(hours=interval_hours)


def take_metadata_snapshot(cluster: ObjectStorageCluster) -> None:
	"""Ask every node for a snapshot. Stamped even when some refuse: retrying before the interval
	would rotate away the good nodes' older snapshot."""
	garage = cluster.garage_setup
	refused = garage.create_metadata_snapshot().get("error") or {}
	cluster.db_set("last_metadata_snapshot_at", now_datetime())

	if refused:
		machines = dict(garage.get_layout_nodes())
		frappe.log_error(
			title=f"{cluster.name}: some nodes did not snapshot their metadata",
			message="\n".join(
				f"{machines.get(node_id, node_id)}: {reason}" for node_id, reason in refused.items()
			),
		)
