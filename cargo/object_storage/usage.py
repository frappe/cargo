from __future__ import annotations

from datetime import datetime, timedelta
from functools import cached_property

import frappe
import requests

from cargo.object_storage.client import Client, Error, NoGateway, Unreachable
from cargo.object_storage.clock import as_utc, site_time, utc_now
from cargo.object_storage.doctype.bucket_usage_day.bucket_usage_day import record_peaks

SETTINGS = "Cargo Settings"
DEFAULT_SAMPLE_MINUTES = 15
PAGE_SIZE = 500
CHUNK_SIZE = 50
SAMPLE_TIMEOUT_SECONDS = 5
TIMEOUTS_BEFORE_STOP = 3
LOG_KEY = "cargo-bucket-usage-sample-logged"
LOG_SECONDS = 3600


class TooSlow(Exception):
	"""Reads in a row timed out: the gateway takes calls but does not answer them."""


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
	if last == datetime.min:
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


def database_errors() -> tuple[type[Exception], ...]:
	"""Errors the job must fail on, so Frappe rolls back and the lock or deadlock is retried."""
	db = frappe.db
	return (
		frappe.QueryTimeoutError,
		frappe.QueryDeadlockError,
		db.OperationalError,
		db.InternalError,
		db.InterfaceError,
		db.ProgrammingError,
		db.DataError,
	)


class PageSample:
	"""One job's read of a page of buckets. Readings are committed chunk by chunk, so a page
	that stops midway keeps what it read, and a cluster that cannot be reached ends the page."""

	def __init__(self, cluster: str) -> None:
		self.cluster = cluster
		self.failed: list[str] = []
		self.timeouts = 0

	@cached_property
	def garage(self) -> Client:
		cluster = frappe.get_cached_doc("Object Storage Cluster", self.cluster)
		return Client(cluster, timeout=SAMPLE_TIMEOUT_SECONDS)

	def run(self, names: list[str]) -> None:
		try:
			for start in range(0, len(names), CHUNK_SIZE):
				self.read_chunk(names[start : start + CHUNK_SIZE])
		except (Unreachable, NoGateway) as error:
			self.log_stopped("not reachable", str(error))
		except TooSlow as error:
			self.log_stopped("reads too slow", str(error))
		except database_errors():
			raise
		except Exception:
			self.log_stopped("failed", frappe.get_traceback())
		self.log_failed()

	def read_chunk(self, names: list[str]) -> None:
		readings: list[tuple[datetime, str, int]] = []
		try:
			for name in names:
				read_at = utc_now()
				if info := self.read(name):
					readings.append((read_at, name, info["bytes"]))
		finally:
			record_peaks(readings)
			frappe.db.commit()  # nosemgrep

	def read(self, name: str) -> dict | None:
		try:
			info = self.garage.bucket(name)
		except Unreachable:
			raise
		except Error as error:
			self.failed.append(f"{name}: {error}")
			self.stop_when_too_slow(error)
			return None

		self.timeouts = 0
		return info

	def stop_when_too_slow(self, error: Error) -> None:
		"""A gateway that takes calls but never answers ends the page, like one that refuses them."""
		self.timeouts = self.timeouts + 1 if isinstance(error.__cause__, requests.Timeout) else 0
		if self.timeouts >= TIMEOUTS_BEFORE_STOP:
			raise TooSlow(f"{self.timeouts} reads in a row timed out: {error}")

	def log_stopped(self, reason: str, message: str) -> None:
		self.log_hourly(f"Stopped reading bucket usage on {self.cluster}: {reason}", message)

	def log_failed(self) -> None:
		if self.failed:
			self.log_hourly(
				f"Could not read usage of {len(self.failed)} buckets on {self.cluster}",
				"\n".join(self.failed),
			)

	def log_hourly(self, title: str, message: str) -> None:
		"""An Error Log at most once an hour per cluster: each page each interval would repeat it."""
		key = f"{LOG_KEY}:{self.cluster}"
		if frappe.cache.get_value(key):
			frappe.logger("cargo.usage").warning(f"{title}: {message}")
			return

		frappe.cache.set_value(key, 1, expires_in_sec=LOG_SECONDS)
		frappe.log_error(title=title, message=message)
