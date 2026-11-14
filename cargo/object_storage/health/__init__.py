from __future__ import annotations

import typing

import frappe

from cargo.object_storage.health.live import UNKNOWN, LiveHealth, prune_history
from cargo.object_storage.telemetry import ship_health

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)
	from cargo.object_storage.doctype.object_storage_cluster.setup import Setup

__all__ = [
	"LiveHealth",
	"prune_history",
	"queue_cluster_metrics_exporters",
	"refresh_health",
]


def live_clusters() -> list[str]:
	return frappe.get_all("Object Storage Cluster", filters={"activated_on": ("is", "set")}, pluck="name")


def refresh_health() -> None:
	"""Re-read every live cluster's health from its gateway, and send it to Datum. Scheduled in `hooks.py`.
	A cluster whose health cannot be read reports Unknown, so a broken check does not look healthy."""
	health = {}
	for name in live_clusters():
		cluster: ObjectStorageCluster = frappe.get_doc("Object Storage Cluster", name)
		try:
			health[name] = LiveHealth(cluster).record().severity
		except Exception:
			frappe.log_error(title=f"Could not read {name} health")
			health[name] = UNKNOWN
	ship_health(health)


def queue_cluster_metrics_exporters(cluster: str) -> None:
	"""Re-install Vector on one cluster's nodes in the background."""
	frappe.enqueue(
		install_cluster_metrics_exporters,
		cluster=cluster,
		queue="long",
		job_id=f"install-metrics-exporters:{cluster}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def install_cluster_metrics_exporters(cluster: str) -> None:
	"""One node failing must not keep the exporter from the rest."""
	garage: Setup = frappe.get_doc("Object Storage Cluster", cluster).garage_setup
	for machine in garage.machines:
		try:
			garage.install_metrics_exporter(machine)
		except Exception:
			frappe.log_error(title=f"Could not install the metrics exporter on {machine['name']}")
