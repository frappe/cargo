# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import frappe
from frappe.model.document import Document
from frappe.query_builder import Criterion, CustomFunction
from frappe.query_builder.functions import Count, Min
from frappe.utils import convert_utc_to_system_timezone, get_datetime, get_system_timezone
from pypika.terms import Case, Values

USAGE_DAY = "Bucket Usage Day"
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


class BucketUsageDay(Document):
	"""One bucket's peak bytes on one UTC day: Cargo's outbox of storage usage for Central."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		bucket_name: DF.Data
		confirmed_at: DF.Datetime | None
		confirmed_bytes: DF.LongInt
		day: DF.Date
		peak_bytes: DF.LongInt
		reason: DF.SmallText | None
		refused: DF.Check
		sampled_at: DF.Datetime | None
		sent_at: DF.Datetime | None
		sent_bytes: DF.LongInt
	# end: auto-generated types

	pass


def on_doctype_update():
	"""A unique index on the pair the name is made from, for looking a day up by bucket."""
	frappe.db.add_unique(USAGE_DAY, ["bucket_name", "day"], constraint_name="unique_bucket_day")


def utc_now() -> datetime:
	return datetime.now(UTC)


def site_time(moment: datetime) -> datetime:
	"""A UTC moment as the naive site time Frappe stores in a Datetime."""
	return convert_utc_to_system_timezone(moment).replace(tzinfo=None)


def as_utc(value: datetime | str) -> datetime:
	"""A stored site time as a UTC moment. An hour a clock change repeats reads as its earlier
	occurrence."""
	return get_datetime(value).replace(tzinfo=ZoneInfo(get_system_timezone())).astimezone(UTC)


def record_peaks(readings: dict[str, int], at: datetime) -> None:
	"""Keep the highest reading of each bucket for the UTC date of `at`, the moment before the
	reads, in one statement. The upsert meets an existing day on its name, `{day}-{bucket}`."""
	if not readings:
		return

	sampled_at = site_time(at)
	day = at.date()
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


def pending(after: str, limit: int) -> list[frappe._dict]:
	"""The next pending days by name, after `after`."""
	table = frappe.qb.DocType(USAGE_DAY)
	return (
		frappe.qb.from_(table)
		.select(table.name, table.bucket_name, table.day, table.peak_bytes, table.sampled_at)
		.where(pending_filter() & (table.name > after))
		.orderby(table.name)
		.limit(limit)
		.run(as_dict=True)
	)


def mark_sent(rows: list[frappe._dict], at: datetime) -> None:
	"""Record the peak each row carried when Central answered for it."""
	table = frappe.qb.DocType(USAGE_DAY)
	sent_bytes = Case()
	for row in rows:
		sent_bytes = sent_bytes.when(table.name == row.name, row.peak_bytes)
	(
		frappe.qb.update(table)
		.set(table.sent_bytes, sent_bytes)
		.set(table.sent_at, at)
		.where(table.name.isin([row.name for row in rows]))
	).run()


def confirm(names: list[str], at: datetime) -> None:
	"""Central took these days at the peak last sent."""
	if not names:
		return

	table = frappe.qb.DocType(USAGE_DAY)
	(
		frappe.qb.update(table)
		.set(table.confirmed_bytes, table.sent_bytes)
		.set(table.confirmed_at, at)
		.where(table.name.isin(names))
	).run()


def refuse(reasons: dict[str, str | None]) -> None:
	"""Central will never take these days; each keeps the reason it gave."""
	by_reason: dict[str | None, list[str]] = {}
	for name, reason in reasons.items():
		by_reason.setdefault(reason, []).append(name)

	table = frappe.qb.DocType(USAGE_DAY)
	for reason, names in by_reason.items():
		(
			frappe.qb.update(table)
			.set(table.refused, 1)
			.set(table.reason, reason)
			.where(table.name.isin(names))
		).run()


def prune() -> None:
	"""Drop days past the outbox window, sent or not. Scheduled hourly."""
	table = frappe.qb.DocType(USAGE_DAY)
	cutoff = utc_now().date() - timedelta(days=OUTBOX_DAYS)
	frappe.qb.from_(table).delete().where(table.day < cutoff).run()


def outbox_state() -> tuple[int, date | None]:
	"""How many days Central has never answered for, and the oldest of them: never sent, or
	read higher since the last send. Days Central asked for again are not counted."""
	table = frappe.qb.DocType(USAGE_DAY)
	unanswered = (table.refused == 0) & Criterion.any(
		[table.sent_at.isnull(), table.peak_bytes > table.sent_bytes]
	)
	depth, oldest = (
		frappe.qb.from_(table).select(Count(table.name), Min(table.day)).where(unanswered).run()[0]
	)
	return depth, oldest
