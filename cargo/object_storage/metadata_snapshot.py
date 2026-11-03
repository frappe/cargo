from __future__ import annotations

import typing
from datetime import UTC, datetime, timedelta

import frappe
from frappe.utils import get_datetime, now_datetime

from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import get_running_workflow
from cargo.object_storage.doctype.object_storage_cluster.setup import BACKUP_SSH_TIMEOUT

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
	# Garage's own format, so one name covers every node of this run.
	# The snapshot name that garage uses is temporary and we replace it with our name
	# While uploading it to offsite, since garage has no admin API returning the snapshot names.
	name = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
	refused = garage.create_metadata_snapshot().get("error") or {}
	machines = dict(garage.get_layout_nodes()) if refused else {}
	if garage.has_metadata_backup:
		queue_metadata_uploads(cluster, name, skip={machines.get(node_id) for node_id in refused})
	cluster.db_set("last_metadata_snapshot_at", now_datetime())

	if refused:
		frappe.log_error(
			title=f"{cluster.name}: some nodes did not snapshot their metadata",
			message="\n".join(
				f"{machines.get(node_id, node_id)}: {reason}" for node_id, reason in refused.items()
			),
		)


def queue_metadata_uploads(cluster: ObjectStorageCluster, name: str, skip: set[str | None]) -> None:
	"""One long job per machine, so a slow upload neither outlives this scheduler job nor holds up
	the others. A node that refused this run is skipped: its newest snapshot is an older one."""
	for machine in cluster.garage_setup.machines:
		if machine["name"] not in skip:
			frappe.enqueue(
				upload_metadata_snapshot,
				queue="long",
				timeout=BACKUP_SSH_TIMEOUT + 180,
				cluster_name=cluster.name,
				machine_name=machine["name"],
				snapshot_name=name,
			)


def upload_metadata_snapshot(cluster_name: str, machine_name: str, snapshot_name: str) -> None:
	"""Old backups are pruned only after a good upload."""
	garage = frappe.get_doc("Object Storage Cluster", cluster_name).garage_setup
	machine = garage.machine(machine_name)
	try:
		garage.upload_metadata_snapshots(machine, snapshot_name)
		garage.prune_metadata_backups(machine)
	except Exception:
		frappe.log_error(title=f"{machine_name}: could not upload metadata snapshots")
