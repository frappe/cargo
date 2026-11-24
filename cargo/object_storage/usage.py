from __future__ import annotations

from datetime import datetime, timedelta

import frappe

from cargo.object_storage.client import Client, Error, Unreachable
from cargo.object_storage.doctype.bucket_usage_day.bucket_usage_day import (
	as_utc,
	record_peaks,
	site_time,
	utc_now,
)

SETTINGS = "Cargo Settings"
DEFAULT_SAMPLE_MINUTES = 15
PAGE_SIZE = 500
CHUNK_SIZE = 50


def sample_usage() -> None:
	"""Queue a read of every bucket once the sample interval has passed. Scheduled each minute."""
	now = utc_now().replace(second=0, microsecond=0)
	if not is_sample_due(now):
		return

	frappe.db.set_single_value(SETTINGS, "last_usage_sample_at", site_time(now), update_modified=False)
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
	if not last or last == datetime.min:
		return True

	minutes = frappe.db.get_single_value(SETTINGS, "usage_sample_minutes") or DEFAULT_SAMPLE_MINUTES
	return now - as_utc(last) >= timedelta(minutes=minutes)


def bucket_pages() -> list[tuple[str, list[str]]]:
	"""Each active cluster's bucket names, in pages small enough for one job."""
	active = frappe.get_all("Object Storage Cluster", filters={"status": "Active"}, pluck="name")
	if not active:
		return []

	buckets = frappe.get_all(
		"Bucket", filters={"cluster": ("in", active)}, fields=["name", "cluster"], order_by="cluster, name"
	)
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
	PageSample(cluster).run(names)


class PageSample:
	"""One job's read of a page of buckets. Readings are committed chunk by chunk, so a job
	killed midway keeps what it read, and a cluster that cannot be reached ends the page."""

	def __init__(self, cluster: str) -> None:
		self.cluster = cluster
		self.garage = Client(frappe.get_cached_doc("Object Storage Cluster", cluster))
		self.failed: list[str] = []

	def run(self, names: list[str]) -> None:
		try:
			for start in range(0, len(names), CHUNK_SIZE):
				self.read_chunk(names[start : start + CHUNK_SIZE])
		except Unreachable as error:
			frappe.log_error(
				title=f"Stopped reading bucket usage on {self.cluster}: not reachable", message=str(error)
			)
		self.log_failed()

	def read_chunk(self, names: list[str]) -> None:
		read_at = utc_now()
		readings: dict[str, int] = {}
		try:
			for name in names:
				if info := self.read(name):
					readings[name] = info["bytes"]
		finally:
			record_peaks(readings, read_at)
			frappe.db.commit()  # nosemgrep

	def read(self, name: str) -> dict | None:
		try:
			return self.garage.bucket(name)
		except Unreachable:
			raise
		except Error as error:
			self.failed.append(f"{name}: {error}")
			return None

	def log_failed(self) -> None:
		if self.failed:
			frappe.log_error(
				title=f"Could not read usage of {len(self.failed)} buckets on {self.cluster}",
				message="\n".join(self.failed),
			)
