from __future__ import annotations

from datetime import datetime, timedelta

import frappe
from frappe.utils import get_datetime

from cargo.object_storage.client import Client, Error
from cargo.object_storage.doctype.bucket_usage_day.bucket_usage_day import record_peaks, site_time, utc_now

SETTINGS = "Cargo Settings"
DEFAULT_SAMPLE_MINUTES = 15
PAGE_SIZE = 500


def sample_usage() -> None:
	"""Queue a read of every bucket once the sample interval has passed. Scheduled each minute."""
	now = utc_now().replace(second=0, microsecond=0)
	if not is_sample_due(now):
		return

	frappe.db.set_single_value(SETTINGS, "last_usage_sample_at", site_time(now))
	for cluster, names in bucket_pages():
		frappe.enqueue(
			sample_page,
			cluster=cluster,
			names=names,
			queue="long",
			job_id=f"bucket-usage-sample:{cluster}:{names[0]}",
			deduplicate=True,
			enqueue_after_commit=True,
		)


def is_sample_due(now: datetime) -> bool:
	last = frappe.db.get_single_value(SETTINGS, "last_usage_sample_at")
	if not last:
		return True

	minutes = frappe.db.get_single_value(SETTINGS, "usage_sample_minutes") or DEFAULT_SAMPLE_MINUTES
	return site_time(now) - get_datetime(last) >= timedelta(minutes=minutes)


def bucket_pages() -> list[tuple[str, list[str]]]:
	"""Each cluster's bucket names, in pages small enough for one job."""
	buckets = frappe.get_all("Bucket", fields=["name", "cluster"], order_by="cluster, name")
	by_cluster: dict[str, list[str]] = {}
	for bucket in buckets:
		by_cluster.setdefault(bucket.cluster, []).append(bucket.name)

	return [
		(cluster, names[start : start + PAGE_SIZE])
		for cluster, names in by_cluster.items()
		for start in range(0, len(names), PAGE_SIZE)
	]


def sample_page(cluster: str, names: list[str]) -> None:
	"""Read each bucket's bytes from Garage and keep the day's peak. Reads the cluster, never
	the Bucket record, so a bucket being worked on is not waited for."""
	garage = Client(frappe.get_cached_doc("Object Storage Cluster", cluster))
	readings: dict[str, int] = {}
	failed: list[str] = []
	for name in names:
		try:
			info = garage.bucket(name)
		except Error as error:
			failed.append(f"{name}: {error}")
			continue
		if info:
			readings[name] = info["bytes"]

	record_peaks(readings)
	if failed:
		frappe.log_error(
			title=f"Could not read usage of {len(failed)} buckets on {cluster}", message="\n".join(failed)
		)
