import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase
from frappe.utils.password import remove_encrypted_password, set_encrypted_password

from cargo.object_storage.usage import USAGE_DAY, record_peaks
from cargo.object_storage.usage_report import UNCONFIGURED_KEY, report_usage
from cargo.testing import use_test_settings

SECRET = "storage-service-secret"
REPORT_URL = "http://central.test/api/method/central.billing.api.metering.report"
SAMPLED = datetime(2026, 11, 24, 10, 15, 30, tzinfo=UTC)


def reply(status_code: int = 200, message: dict | None = None) -> MagicMock:
	response = MagicMock(status_code=status_code, ok=status_code < 400, text="")
	response.json.return_value = {"message": message}
	return response


class Central:
	"""Central's report endpoint: accepts every line but those it is told to reject."""

	def __init__(self, rejects: dict[str, bool] | None = None):
		self.rejects = rejects or {}
		self.calls: list[dict] = []

	def __call__(self, url, data, headers, timeout):
		self.calls.append({"url": url, "body": data, "headers": headers, "report": json.loads(data)})
		lines = json.loads(data)["lines"]
		rejected = [
			{**line, "dimension": "", "reason": "unknown subject", "retry": self.rejects[line["subject"]]}
			for line in lines
			if line["subject"] in self.rejects
		]
		return reply(message={"accepted": len(lines) - len(rejected), "rejected": rejected})

	def subjects(self, call: int = -1) -> list[str]:
		return [line["subject"] for line in self.calls[call]["report"]["lines"]]


class IntegrationTestBucketUsageReport(IntegrationTestCase):
	"""What Cargo sends Central each hour, and what it does with the answer."""

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("bucket_usage_report_test")
		self.addCleanup(frappe.db.rollback, save_point="bucket_usage_report_test")
		use_test_settings()
		frappe.db.delete(USAGE_DAY)
		frappe.db.set_single_value("Cargo Settings", "pause_usage_report", 0)
		set_encrypted_password("Cargo Settings", "Cargo Settings", SECRET, "central_report_secret")
		frappe.cache.delete_value(UNCONFIGURED_KEY)

	def sample(self, readings: dict[str, int], moment: datetime = SAMPLED) -> None:
		with patch("cargo.object_storage.usage.utc_now", return_value=moment):
			record_peaks(readings)

	def report(self, central) -> MagicMock:
		with patch("cargo.object_storage.usage_report.requests.post", side_effect=central) as post:
			report_usage()
		return post

	def day(self, bucket: str) -> frappe._dict:
		return frappe.db.get_value(
			USAGE_DAY,
			{"bucket_name": bucket},
			["confirmed_at", "confirmed_bytes", "refused", "reason"],
			as_dict=True,
		)

	def test_a_report_is_signed_with_the_report_secret(self):
		self.sample({"alpha": 2048})
		central = Central()

		self.report(central)

		call = central.calls[0]
		expected = base64.b64encode(hmac.new(SECRET.encode(), call["body"], hashlib.sha256).digest()).decode()
		self.assertEqual(call["url"], REPORT_URL)
		self.assertEqual(call["headers"]["X-Central-Signature"], expected)
		self.assertEqual(call["headers"]["X-Central-Service"], "storage")
		self.assertEqual(
			call["report"],
			{
				"as_of": "2026-11-24T10:15:30Z",
				"lines": [{"subject": "alpha", "meter": "bytes_stored", "day": "2026-11-24", "value": 2048}],
			},
		)

	def test_as_of_is_the_newest_sample_in_the_batch(self):
		self.sample({"alpha": 1}, SAMPLED.replace(day=23))
		self.sample({"beta": 1})

		central = Central()
		self.report(central)

		self.assertEqual(central.calls[0]["report"]["as_of"], "2026-11-24T10:15:30Z")

	def test_a_report_carries_at_most_a_thousand_lines(self):
		self.sample({f"bucket-{index:04d}": index for index in range(1001)})
		central = Central()

		self.report(central)

		self.assertEqual([len(call["report"]["lines"]) for call in central.calls], [1000, 1])
		self.assertEqual(frappe.db.count(USAGE_DAY, {"confirmed_at": ("is", "set")}), 1001)

	def test_accepted_days_are_confirmed_and_not_sent_again(self):
		self.sample({"alpha": 2048})
		self.report(Central())

		self.assertEqual(self.day("alpha").confirmed_bytes, 2048)
		self.assertFalse(self.report(Central()).called)

	def test_a_day_whose_peak_rose_after_it_was_confirmed_is_sent_again(self):
		self.sample({"alpha": 2048})
		self.report(Central())
		self.sample({"alpha": 4096})

		central = Central()
		self.report(central)

		self.assertEqual(central.calls[0]["report"]["lines"][0]["value"], 4096)
		self.assertEqual(self.day("alpha").confirmed_bytes, 4096)

	def test_a_line_central_asks_for_again_is_sent_again(self):
		self.sample({"alpha": 1, "beta": 1})
		self.report(Central({"beta": True}))

		self.assertIsNone(self.day("beta").confirmed_at)
		central = Central()
		self.report(central)
		self.assertEqual(central.subjects(), ["beta"])

	def test_a_line_central_will_never_take_is_not_sent_again(self):
		self.sample({"alpha": 1, "atlas-bucket": 1})
		self.report(Central({"atlas-bucket": False}))

		refused = self.day("atlas-bucket")
		self.assertTrue(refused.refused)
		self.assertEqual(refused.reason, "unknown subject")
		self.sample({"atlas-bucket": 5})
		self.assertFalse(self.report(Central()).called)

	def test_days_wait_while_central_is_down(self):
		self.sample({"alpha": 1})
		down = (
			requests.ConnectionError("refused"),
			requests.Timeout("slow"),
			lambda *args, **kwargs: reply(503),
		)
		for failure in down:
			with self.subTest(failure=failure):
				self.report(failure)

				self.assertIsNone(self.day("alpha").confirmed_at)

		central = Central()
		self.report(central)
		self.assertEqual(central.subjects(), ["alpha"])

	def test_a_report_central_refuses_whole_is_logged_and_kept(self):
		self.sample({"alpha": 1})

		with patch("cargo.object_storage.usage_report.frappe.log_error") as log_error:
			self.report(lambda *args, **kwargs: reply(403))

		log_error.assert_called_once()
		self.assertIsNone(self.day("alpha").confirmed_at)

	def test_a_paused_report_sends_nothing(self):
		self.sample({"alpha": 1})
		frappe.db.set_single_value("Cargo Settings", "pause_usage_report", 1)

		self.assertFalse(self.report(Central()).called)

	def test_a_cargo_without_a_report_secret_sends_nothing(self):
		self.sample({"alpha": 1})
		remove_encrypted_password("Cargo Settings", "Cargo Settings", "central_report_secret")

		self.assertFalse(self.report(Central()).called)

	def test_a_cargo_without_a_central_url_sends_nothing(self):
		self.sample({"alpha": 1})
		frappe.db.set_single_value("Cargo Settings", "central_url", "")

		self.assertFalse(self.report(Central()).called)
