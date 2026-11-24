from datetime import UTC, date, datetime
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.object_storage.client import Client, Error
from cargo.object_storage.usage import (
	USAGE_DAY,
	bucket_pages,
	outbox_state,
	prune_usage_days,
	record_peaks,
	sample_page,
	sample_usage,
	site_time,
)

NOON = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


class IntegrationTestBucketUsage(IntegrationTestCase):
	"""How Cargo keeps each bucket's peak for the UTC day."""

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("bucket_usage_test")
		self.addCleanup(frappe.db.rollback, save_point="bucket_usage_test")
		frappe.db.delete(USAGE_DAY)
		frappe.db.delete("Bucket")
		self.cluster = self.make_cluster()

	def make_cluster(self) -> str:
		name = f"test-cluster-{frappe.generate_hash(length=6)}"
		frappe.get_doc({"doctype": "Object Storage Cluster", "name": name}).db_insert()
		return name

	def make_bucket(self, name: str, cluster: str | None = None) -> None:
		"""A record only: the provisioning it runs on insert is not what is tested here."""
		frappe.get_doc(
			{"doctype": "Bucket", "name": name, "bucket_name": name, "cluster": cluster or self.cluster}
		).db_insert()

	def record_at(self, moment: datetime, readings: dict[str, int]) -> None:
		with patch("cargo.object_storage.usage.utc_now", return_value=moment):
			record_peaks(readings)

	def peak(self, bucket: str, day: date) -> int | None:
		return frappe.db.get_value(USAGE_DAY, {"bucket_name": bucket, "day": day}, "peak_bytes")

	def test_a_day_keeps_its_highest_reading(self):
		for used_bytes in (100, 300, 50):
			self.record_at(NOON, {"alpha": used_bytes})

		self.assertEqual(self.peak("alpha", NOON.date()), 300)
		self.assertEqual(frappe.db.count(USAGE_DAY, {"bucket_name": "alpha"}), 1)

	def test_a_reading_after_midnight_utc_starts_a_new_day(self):
		"""The day is the UTC date, whatever the site's time zone."""
		self.record_at(datetime(2026, 11, 24, 23, 59, tzinfo=UTC), {"alpha": 500})
		self.record_at(datetime(2026, 11, 25, 0, 1, tzinfo=UTC), {"alpha": 20})

		self.assertEqual(self.peak("alpha", date(2026, 11, 24)), 500)
		self.assertEqual(self.peak("alpha", date(2026, 11, 25)), 20)

	def test_a_day_records_when_it_was_last_read(self):
		self.record_at(NOON, {"alpha": 100})
		later = NOON.replace(hour=13)
		self.record_at(later, {"alpha": 10})

		sampled_at = frappe.db.get_value(USAGE_DAY, {"bucket_name": "alpha"}, "sampled_at")
		self.assertEqual(sampled_at, site_time(later))

	def test_a_bucket_that_cannot_be_read_does_not_stop_the_others(self):
		def garage(alias):
			if alias == "broken":
				raise Error("GetBucketInfo answered 500: internal")
			return {"id": alias, "bytes": 42}

		with (
			patch.object(Client, "bucket", side_effect=garage),
			patch("cargo.object_storage.usage.frappe.log_error") as log_error,
			patch("cargo.object_storage.usage.utc_now", return_value=NOON),
		):
			sample_page(self.cluster, ["alpha", "broken", "gamma"])

		self.assertEqual(self.peak("alpha", NOON.date()), 42)
		self.assertEqual(self.peak("gamma", NOON.date()), 42)
		self.assertIsNone(self.peak("broken", NOON.date()))
		log_error.assert_called_once()

	def test_a_bucket_garage_no_longer_has_is_skipped(self):
		with (
			patch.object(Client, "bucket", return_value=None),
			patch("cargo.object_storage.usage.utc_now", return_value=NOON),
		):
			sample_page(self.cluster, ["gone"])

		self.assertFalse(frappe.db.exists(USAGE_DAY, {"bucket_name": "gone"}))

	def test_buckets_are_read_per_cluster(self):
		"""A region can have more than one cluster, and each answers for its own buckets."""
		other = self.make_cluster()
		self.make_bucket("alpha")
		self.make_bucket("beta", other)

		self.assertEqual(sorted(bucket_pages()), sorted([(self.cluster, ["alpha"]), (other, ["beta"])]))

	def sample_at(self, moment: datetime) -> list:
		with (
			patch("cargo.object_storage.usage.utc_now", return_value=moment),
			patch("cargo.object_storage.usage.frappe.enqueue") as enqueue,
		):
			sample_usage()
		return [call.kwargs["names"] for call in enqueue.call_args_list]

	def test_buckets_are_read_once_the_interval_has_passed(self):
		self.make_bucket("alpha")
		frappe.db.set_single_value("Cargo Settings", "usage_sample_minutes", 15)
		frappe.db.set_single_value("Cargo Settings", "last_usage_sample_at", None)

		self.assertEqual(self.sample_at(NOON), [["alpha"]])
		self.assertEqual(self.sample_at(NOON.replace(minute=14)), [])
		self.assertEqual(self.sample_at(NOON.replace(minute=15)), [["alpha"]])

	def test_the_interval_is_a_setting(self):
		self.make_bucket("alpha")
		frappe.db.set_single_value("Cargo Settings", "usage_sample_minutes", 5)
		frappe.db.set_single_value("Cargo Settings", "last_usage_sample_at", site_time(NOON))

		self.assertEqual(self.sample_at(NOON.replace(minute=4)), [])
		self.assertEqual(self.sample_at(NOON.replace(minute=5)), [["alpha"]])

	def test_days_past_the_outbox_window_are_dropped(self):
		"""Thirty days, whether Central confirmed them or not."""
		self.record_at(datetime(2026, 10, 24, 12, 0, tzinfo=UTC), {"old": 1, "confirmed": 1})
		self.record_at(datetime(2026, 10, 25, 12, 0, tzinfo=UTC), {"kept": 1})
		frappe.db.set_value(
			USAGE_DAY, {"bucket_name": "confirmed"}, {"confirmed_at": site_time(NOON), "confirmed_bytes": 1}
		)

		with patch("cargo.object_storage.usage.utc_now", return_value=NOON):
			prune_usage_days()

		self.assertEqual(frappe.get_all(USAGE_DAY, pluck="bucket_name"), ["kept"])

	def test_the_outbox_counts_days_waiting_for_central(self):
		self.record_at(datetime(2026, 11, 20, 12, 0, tzinfo=UTC), {"alpha": 1})
		self.record_at(NOON, {"alpha": 5, "beta": 5})
		frappe.db.set_value(
			USAGE_DAY, {"bucket_name": "beta"}, {"confirmed_at": site_time(NOON), "confirmed_bytes": 5}
		)

		self.assertEqual(outbox_state(), (2, date(2026, 11, 20)))
