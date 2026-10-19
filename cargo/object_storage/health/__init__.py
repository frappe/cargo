from __future__ import annotations

import typing

import frappe

from cargo.object_storage.health.live import LiveHealth, prune_history
from cargo.object_storage.health.telemetry import Telemetry
from cargo.telemetry.credential import get_metrics_info

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)
	from cargo.object_storage.doctype.object_storage_cluster.setup import Setup

__all__ = [
	"LiveHealth",
	"Telemetry",
	"install_garage_metrics_exporters",
	"prune_history",
	"refresh_health",
	"ship_metrics",
]


def live_clusters() -> list[str]:
	return frappe.get_all("Object Storage Cluster", filters={"activated_on": ("is", "set")}, pluck="name")


def refresh_health() -> None:
	"""Re-read every live cluster's health from its gateway. Scheduled in `hooks.py`."""
	for name in live_clusters():
		cluster: ObjectStorageCluster = frappe.get_doc("Object Storage Cluster", name)
		try:
			LiveHealth(cluster).record()
		except Exception:
			frappe.log_error(title=f"Could not read {name} health")


def ship_metrics() -> None:
	"""Relay every live cluster's Garage metrics to datum. Scheduled in `hooks.py`.

	A host whose bench has no datum credentials yet has nothing to ship, which is a state
	to wait out rather than report every five minutes."""
	try:
		get_metrics_info()
	except RuntimeError:
		return

	for name in live_clusters():
		cluster: ObjectStorageCluster = frappe.get_doc("Object Storage Cluster", name)
		try:
			Telemetry(cluster).ship()
		except Exception:
			frappe.log_error(title=f"Could not ship {name} metrics")


def install_garage_metrics_exporters() -> None:
	"""Hand every live cluster's nodes the current Datum token, one job per cluster."""
	for name in live_clusters():
		frappe.enqueue(
			install_cluster_metrics_exporters,
			cluster=name,
			queue="long",
			job_id=f"install-metrics-exporters:{name}",
			deduplicate=True,
			enqueue_after_commit=True,
		)


def install_cluster_metrics_exporters(cluster: str) -> None:
	"""One node failing must not keep the token from the rest."""
	garage: Setup = frappe.get_doc("Object Storage Cluster", cluster).garage_setup
	for machine in garage.machines:
		try:
			garage.install_metrics_exporter(machine)
		except Exception:
			frappe.log_error(title=f"Could not install the metrics exporter on {machine['name']}")
