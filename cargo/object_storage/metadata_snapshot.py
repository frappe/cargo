from __future__ import annotations

import typing

import frappe
from frappe.utils import now_datetime

from cargo.object_storage.doctype.object_storage_cluster.setup import BACKUP_SSH_TIMEOUT
from cargo.object_storage.doctype.object_storage_cluster.utils import get_running_workflow

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)


def upload_metadata_snapshots() -> None:
	"""Back up every machine of the live cluster offsite. Garage takes the snapshots on its own
	clock, so this only ships what it finished. Scheduled in `hooks.py`."""
	cluster_name = frappe.db.get_value("Object Storage Cluster", {"status": "Active"})
	if not cluster_name:
		return

	cluster: ObjectStorageCluster = frappe.get_doc("Object Storage Cluster", cluster_name)
	if not cluster.garage_setup.has_metadata_backup or get_running_workflow(cluster):
		return

	# One long job per machine, so a slow upload neither outlives this scheduler job nor holds
	# up the others.
	for machine in cluster.garage_setup.machines:
		frappe.enqueue(
			upload_metadata_snapshot,
			queue="long",
			timeout=BACKUP_SSH_TIMEOUT + 180,
			job_id=f"upload-metadata-snapshot-{machine['name']}",
			deduplicate=True,
			cluster_name=cluster.name,
			machine_name=machine["name"],
		)


def upload_metadata_snapshot(cluster_name: str, machine_name: str) -> None:
	"""Upload the machine's newest finished snapshot unless the bucket has it. Old backups are
	pruned only after a good upload."""
	cluster: ObjectStorageCluster = frappe.get_doc("Object Storage Cluster", cluster_name)
	garage = cluster.garage_setup
	machine = garage.machine(machine_name)
	try:
		snapshot = garage.get_newest_metadata_snapshot(machine)
		if not snapshot or garage.has_metadata_backup_of(machine, snapshot):
			return

		garage.upload_metadata_snapshot(machine, snapshot)
		garage.prune_metadata_backups(machine)
	except Exception:
		frappe.log_error(title=f"{machine_name}: could not upload metadata snapshot")
		return

	cluster.db_set("last_metadata_snapshot_at", now_datetime(), update_modified=False)
