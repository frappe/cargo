from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import frappe
from frappe.query_builder import Criterion, CustomFunction
from frappe.query_builder.functions import Count, Min
from frappe.utils import convert_utc_to_system_timezone, get_datetime
from pypika.terms import Values

from cargo.object_storage.client import Client, Error

SETTINGS = "Cargo Settings"
USAGE_DAY = "Bucket Usage Day"
DEFAULT_SAMPLE_MINUTES = 15
PAGE_SIZE = 500
OUTBOX_DAYS = 30
OWNER = "Administrator"
USAGE_COLUMNS = (
	"name",
	"bucket_name",
	"day",
	"peak_bytes",
	"sampled_at",
	"creation",
	"modified",
	"owner",
	"modified_by",
)

Greatest = CustomFunction("GREATEST", ["first", "second"])


def utc_now() -> datetime:
	return datetime.now(UTC)


def site_time(moment: datetime) -> datetime:
	"""A UTC moment as the naive site time Frappe stores in a Datetime."""
	return convert_utc_to_system_timezone(moment).replace(tzinfo=None)


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


def record_peaks(readings: dict[str, int]) -> None:
	"""Keep the highest reading of each bucket for today's UTC date, in one statement."""
	if not readings:
		return

	now = utc_now()
	sampled_at = site_time(now)
	day = now.date()
	table = frappe.qb.DocType(USAGE_DAY)
	rows = [
		(f"{day}-{bucket}", bucket, day, used_bytes, sampled_at, sampled_at, sampled_at, OWNER, OWNER)
		for bucket, used_bytes in readings.items()
	]
	(
		frappe.qb.into(table)
		.columns(*USAGE_COLUMNS)
		.insert(*rows)
		.on_duplicate_key_update(table.peak_bytes, Greatest(table.peak_bytes, Values(table.peak_bytes)))
		.on_duplicate_key_update(table.sampled_at, Greatest(table.sampled_at, Values(table.sampled_at)))
		.on_duplicate_key_update(table.modified, Values(table.modified))
	).run()


def pending_filter():
	"""Days Central has not confirmed, or confirmed at a lower peak, and has not refused."""
	table = frappe.qb.DocType(USAGE_DAY)
	return (table.refused == 0) & Criterion.any(
		[table.confirmed_at.isnull(), table.peak_bytes > table.confirmed_bytes]
	)


def prune_usage_days() -> None:
	"""Drop days past the outbox window, sent or not. Scheduled hourly."""
	table = frappe.qb.DocType(USAGE_DAY)
	cutoff = utc_now().date() - timedelta(days=OUTBOX_DAYS)
	frappe.qb.from_(table).delete().where(table.day < cutoff).run()


def outbox_state() -> tuple[int, date | None]:
	"""How many days wait for Central, and the oldest of them."""
	table = frappe.qb.DocType(USAGE_DAY)
	depth, oldest = (
		frappe.qb.from_(table).select(Count(table.name), Min(table.day)).where(pending_filter()).run()[0]
	)
	return depth, oldest
