from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from frappe.utils import convert_utc_to_system_timezone, get_datetime, get_system_timezone


def utc_now() -> datetime:
	return datetime.now(UTC)


def site_time(moment: datetime) -> datetime:
	"""A UTC moment as the naive site time Frappe stores in a Datetime."""
	return convert_utc_to_system_timezone(moment).replace(tzinfo=None)


def as_utc(value: datetime | str) -> datetime:
	"""A stored site time as a UTC moment. An hour a clock change repeats reads as its earlier
	occurrence."""
	return get_datetime(value).replace(tzinfo=ZoneInfo(get_system_timezone())).astimezone(UTC)
