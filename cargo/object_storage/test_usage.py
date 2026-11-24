from datetime import UTC, date, datetime
from unittest.mock import MagicMock, Mock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase
from urllib3.exceptions import MaxRetryError, NewConnectionError

from cargo.object_storage.client import Client, Error, Unreachable
from cargo.object_storage.clock import site_time
from cargo.object_storage.doctype.bucket_usage_day.bucket_usage_day import (
	USAGE_DAY,
	outbox_state,
	prune,
	record_peaks,
)
from cargo.object_storage.usage import (
	CHUNK_SIZE,
	LOG_KEY,
	SAMPLE_TIMEOUT_SECONDS,
	bucket_pages,
	is_sample_due,
	sample_page,
	sample_usage,
)

OUTBOX = "cargo.object_storage.doctype.bucket_usage_day.bucket_usage_day"

NOON = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def answer(used_bytes: int) -> Mock:
	return Mock(ok=True, status_code=200, content=b"{}", json=lambda: {"bytes": used_bytes})


def refused() -> requests.ConnectionError:
	return requests.ConnectionError(MaxRetryError(None, "gateway", NewConnectionError(None, "refused")))


class IntegrationTestBucketUsage(IntegrationTestCase):
	"""How Cargo keeps each bucket's peak for the UTC day."""

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("bucket_usage_test")
		self.addCleanup(frappe.db.rollback, save_point="bucket_usage_test")
		frappe.db.delete(USAGE_DAY)
		frappe.db.delete("Bucket")
		self.cluster = self.make_cluster()
		self.addCleanup(frappe.cache.delete_value, f"{LOG_KEY}:{self.cluster}")

	def make_cluster(self, status: str = "Active") -> str:
		name = f"test-cluster-{frappe.generate_hash(length=6)}"
		frappe.get_doc({"doctype": "Object Storage Cluster", "name": name, "status": status}).db_insert()
		return name

	def make_bucket(self, name: str, cluster: str | None = None) -> None:
		"""A record only: the provisioning it runs on insert is not what is tested here."""
		frappe.get_doc(
			{"doctype": "Bucket", "name": name, "bucket_name": name, "cluster": cluster or self.cluster}
		).db_insert()

	def record_at(self, moment: datetime, readings: dict[str, int]) -> None:
		record_peaks(readings, moment)

	def sample_page_at(self, moment, names: list[str], garage) -> MagicMock:
		"""Sample a page with Garage answering through `garage`, and return the error log."""
		utc_now = {"side_effect": moment} if isinstance(moment, list) else {"return_value": moment}
		with (
			patch.object(Client, "bucket", side_effect=garage),
			patch("cargo.object_storage.usage.frappe.log_error") as log_error,
			patch("cargo.object_storage.usage.frappe.db.commit"),
			patch("cargo.object_storage.usage.utc_now", **utc_now),
		):
			sample_page(self.cluster, names)
		return log_error

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

		log_error = self.sample_page_at(NOON, ["alpha", "broken", "gamma"], garage)

		self.assertEqual(self.peak("alpha", NOON.date()), 42)
		self.assertEqual(self.peak("gamma", NOON.date()), 42)
		self.assertIsNone(self.peak("broken", NOON.date()))
		log_error.assert_called_once()

	def test_buckets_that_cannot_be_read_are_logged_once_an_hour(self):
		"""A cluster answering 500 to every read would otherwise log each page each interval."""

		def failing():
			return Mock(side_effect=Error("GetBucketInfo answered 500: internal"))

		calls = [self.sample_page_at(NOON, ["alpha", "beta"], failing()).call_count for _ in range(3)]

		self.assertEqual(calls, [1, 0, 0])

		frappe.cache.delete_value(f"{LOG_KEY}:{self.cluster}")
		self.sample_page_at(NOON, ["alpha", "beta"], failing()).assert_called_once()

	def test_a_bucket_garage_no_longer_has_is_skipped(self):
		self.sample_page_at(NOON, ["gone"], lambda alias: None)

		self.assertFalse(frappe.db.exists(USAGE_DAY, {"bucket_name": "gone"}))

	def sample_page_over_http(self, names: list[str], answers: list) -> list[str]:
		"""Sample a page with one gateway answering `answers` in turn; return the buckets asked."""
		with (
			patch.object(Client, "urls", ["http://gateway:3903"]),
			patch.object(Client, "headers", {}),
			patch("cargo.object_storage.client.requests.request", side_effect=answers) as self.sent,
			patch("cargo.object_storage.usage.frappe.log_error") as self.log_error,
			patch("cargo.object_storage.usage.frappe.db.commit"),
			patch("cargo.object_storage.usage.utc_now", return_value=NOON),
		):
			sample_page(self.cluster, names)
		return [call.kwargs["params"]["globalAlias"] for call in self.sent.call_args_list]

	def test_an_unreachable_cluster_is_not_read_further(self):
		"""One failed connection ends the page, so a dead cluster holds no worker for hours. What
		was read before it is kept, the chunk it broke off in included."""
		names = [f"bucket-{index:03d}" for index in range(CHUNK_SIZE + 10)]
		answers = [answer(1)] * (CHUNK_SIZE + 2) + [refused()]

		asked = self.sample_page_over_http(names, answers)

		self.assertEqual(asked, names[: CHUNK_SIZE + 3])
		self.assertEqual(frappe.db.count(USAGE_DAY), CHUNK_SIZE + 2)
		self.log_error.assert_called_once()

	def test_a_slow_bucket_is_skipped_and_the_page_goes_on(self):
		"""A read timeout means the gateway was reached, so only that bucket is missed."""
		asked = self.sample_page_over_http(
			["alpha", "slow", "gamma"], [answer(42), requests.ReadTimeout("read timed out"), answer(42)]
		)

		self.assertEqual(asked, ["alpha", "slow", "gamma"])
		self.assertEqual(self.peak("alpha", NOON.date()), 42)
		self.assertEqual(self.peak("gamma", NOON.date()), 42)
		self.assertIsNone(self.peak("slow", NOON.date()))
		self.assertIn("slow", self.log_error.call_args.kwargs["message"])

	def test_a_cluster_without_a_running_gateway_is_not_read(self):
		with (
			patch("cargo.object_storage.usage.frappe.log_error") as log_error,
			patch("cargo.object_storage.usage.frappe.db.commit"),
		):
			sample_page(self.cluster, ["alpha", "beta"])

		log_error.assert_called_once()
		self.assertIn("not reachable", log_error.call_args.kwargs["title"])
		self.assertIn("no running gateway", log_error.call_args.kwargs["message"])
		self.assertEqual(frappe.db.count(USAGE_DAY), 0)

	def test_a_page_that_breaks_midway_keeps_what_it_read_and_is_logged(self):
		"""Any failure, not only an unreachable cluster, ends the page without failing the job."""
		names = [f"bucket-{index:03d}" for index in range(CHUNK_SIZE + 10)]

		def garage(alias):
			if alias == names[-1]:
				raise KeyError("bytes")
			return {"id": alias, "bytes": 1}

		log_error = self.sample_page_at(NOON, names, garage)

		self.assertEqual(frappe.db.count(USAGE_DAY), CHUNK_SIZE + 9)
		log_error.assert_called_once()
		self.assertIn("KeyError", log_error.call_args.kwargs["message"])

	def test_a_reading_belongs_to_the_day_it_was_read_on(self):
		"""Each bucket is timed by its own read, so a chunk that crosses midnight UTC splits."""
		before, after = datetime(2026, 11, 24, 23, 59, tzinfo=UTC), datetime(2026, 11, 25, 0, 1, tzinfo=UTC)

		self.sample_page_at([before, after], ["alpha", "beta"], lambda alias: {"id": alias, "bytes": 7})

		self.assertEqual(self.peak("alpha", before.date()), 7)
		self.assertIsNone(self.peak("alpha", after.date()))
		self.assertEqual(self.peak("beta", after.date()), 7)
		self.assertIsNone(self.peak("beta", before.date()))

	def test_an_unreachable_cluster_is_logged_once_an_hour(self):
		"""Cluster health already reports the outage; each page each interval would repeat it."""
		calls = [
			self.sample_page_at(NOON, ["alpha"], Mock(side_effect=Unreachable("refused"))).call_count
			for _ in range(3)
		]

		self.assertEqual(calls, [1, 0, 0])

		frappe.cache.delete_value(f"{LOG_KEY}:{self.cluster}")
		self.sample_page_at(NOON, ["alpha"], Mock(side_effect=Unreachable("refused"))).assert_called_once()

	def test_a_page_that_breaks_is_logged_once_an_hour(self):
		self.sample_page_at(NOON, ["alpha"], Mock(side_effect=KeyError("bytes"))).assert_called_once()
		self.sample_page_at(NOON, ["alpha"], Mock(side_effect=KeyError("bytes"))).assert_not_called()

	def test_sampling_waits_seconds_not_the_default_for_an_answer(self):
		asked = self.sample_page_over_http(["alpha"], [answer(1)])

		self.assertEqual(asked, ["alpha"])
		self.assertEqual(self.sent.call_args.kwargs["timeout"], SAMPLE_TIMEOUT_SECONDS)
		self.assertEqual(Client(frappe.get_doc("Object Storage Cluster", self.cluster)).timeout, 30)

	def test_a_gateway_that_never_answers_ends_the_page(self):
		"""Reads that time out in a row end the page, keeping what was read before them."""
		names = ["alpha", "slow-1", "slow-2", "slow-3", "omega"]
		timeout = requests.ReadTimeout("read timed out")

		asked = self.sample_page_over_http(names, [answer(1), timeout, timeout, timeout, answer(1)])

		self.assertEqual(asked, names[:4])
		self.assertEqual(self.peak("alpha", NOON.date()), 1)
		titles = [call.kwargs["title"] for call in self.log_error.call_args_list]
		self.assertTrue(any("not reachable" in title for title in titles))

	def test_an_answer_between_timeouts_starts_the_count_again(self):
		names = ["slow-1", "slow-2", "alpha", "slow-3", "slow-4", "omega"]
		timeout = requests.ReadTimeout("read timed out")

		asked = self.sample_page_over_http(names, [timeout, timeout, answer(1), timeout, timeout, answer(1)])

		self.assertEqual(asked, names)
		self.assertEqual(self.peak("omega", NOON.date()), 1)

	def test_buckets_of_a_cluster_not_active_are_not_read(self):
		self.make_bucket("alpha")
		self.make_bucket("beta", self.make_cluster(status="Setting Up"))

		self.assertEqual(bucket_pages(), [(self.cluster, ["alpha"])])

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

	def test_a_sample_leaves_cargo_settings_unmodified(self):
		"""An open Cargo Settings form can still be saved after a sample."""
		frappe.db.set_single_value("Cargo Settings", "last_usage_sample_at", None)
		modified = frappe.db.get_single_value("Cargo Settings", "modified")

		self.sample_at(NOON)

		self.assertEqual(frappe.db.get_single_value("Cargo Settings", "modified"), modified)

	def test_the_interval_holds_across_a_clock_set_back(self):
		"""01:50 EDT is stored as site time; 15 minutes later the clock reads 01:05 EST."""
		frappe.db.set_single_value("Cargo Settings", "usage_sample_minutes", 15)
		frappe.db.set_single_value("Cargo Settings", "last_usage_sample_at", datetime(2026, 11, 1, 1, 50))

		with patch("cargo.object_storage.clock.get_system_timezone", return_value="America/New_York"):
			self.assertFalse(is_sample_due(datetime(2026, 11, 1, 6, 4, tzinfo=UTC)))
			self.assertTrue(is_sample_due(datetime(2026, 11, 1, 6, 5, tzinfo=UTC)))

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

		with patch(f"{OUTBOX}.utc_now", return_value=NOON):
			prune()

		self.assertEqual(frappe.get_all(USAGE_DAY, pluck="bucket_name"), ["kept"])

	def test_the_outbox_counts_days_central_never_answered(self):
		"""Unsent days and days read higher since they were sent; not days Central asked for again."""
		self.record_at(datetime(2026, 11, 20, 12, 0, tzinfo=UTC), {"unsent": 1})
		self.record_at(NOON, {"rose": 5, "retry": 5, "confirmed": 5})
		frappe.db.set_value(USAGE_DAY, {"bucket_name": "rose"}, {"sent_at": site_time(NOON), "sent_bytes": 3})
		frappe.db.set_value(
			USAGE_DAY, {"bucket_name": "retry"}, {"sent_at": site_time(NOON), "sent_bytes": 5}
		)
		frappe.db.set_value(
			USAGE_DAY,
			{"bucket_name": "confirmed"},
			{
				"sent_at": site_time(NOON),
				"sent_bytes": 5,
				"confirmed_at": site_time(NOON),
				"confirmed_bytes": 5,
			},
		)

		self.assertEqual(outbox_state(), (2, date(2026, 11, 20)))
