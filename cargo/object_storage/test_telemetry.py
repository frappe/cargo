from datetime import timedelta
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from cargo.object_storage.health import refresh_health
from cargo.object_storage.telemetry import HEALTH, METADATA_BACKUP_AGE, get_samples, ship_metrics

METRICS_INFO = {"endpoint": "https://datum.test/", "token": "datum-jwt"}


class IntegrationTestObjectStorageTelemetry(IntegrationTestCase):
	"""What Datum learns about object storage backups each hour."""

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("object_storage_telemetry_test")
		self.addCleanup(frappe.db.rollback, save_point="object_storage_telemetry_test")
		# Only the row counts here, so the test skips the setup that a real cluster runs on insert.
		self.cluster = f"test-cluster-{frappe.generate_hash(length=6)}"
		frappe.get_doc({"doctype": "Object Storage Cluster", "name": self.cluster}).db_insert()

	def set_cluster(self, **values):
		frappe.db.set_value("Object Storage Cluster", self.cluster, values, update_modified=False)

	def age_of_cluster(self) -> float | None:
		ages = [sample["value"] for sample in get_samples() if sample["labels"]["cluster"] == self.cluster]
		return ages[0] if ages else None

	def test_the_age_counts_from_the_newest_backup(self):
		self.set_cluster(
			activated_on=now_datetime() - timedelta(days=9),
			last_metadata_snapshot_at=now_datetime() - timedelta(hours=2),
		)

		self.assertAlmostEqual(self.age_of_cluster(), 2 * 3600, delta=60)

	def test_a_cluster_without_a_backup_counts_from_its_activation(self):
		self.set_cluster(activated_on=now_datetime() - timedelta(days=3), last_metadata_snapshot_at=None)

		self.assertAlmostEqual(self.age_of_cluster(), 3 * 86400, delta=60)

	def test_a_cluster_that_is_not_live_is_left_out(self):
		self.set_cluster(activated_on=None)

		self.assertIsNone(self.age_of_cluster())

	def test_samples_are_posted_to_datum(self):
		self.set_cluster(activated_on=now_datetime(), last_metadata_snapshot_at=now_datetime())
		with (
			patch("cargo.telemetry.datum.get_metrics_info", return_value=METRICS_INFO),
			patch("cargo.telemetry.datum.requests.post", return_value=MagicMock()) as post,
		):
			ship_metrics()

		self.assertEqual(post.call_args.args[0], "https://datum.test/v1/ingest")
		self.assertEqual(post.call_args.kwargs["json"]["samples"][0]["metric"], METADATA_BACKUP_AGE)

	def test_a_cluster_whose_health_cannot_be_read_reports_unknown(self):
		self.set_cluster(activated_on=now_datetime())
		with (
			patch("cargo.object_storage.health.LiveHealth.record", side_effect=RuntimeError("gateway down")),
			patch("cargo.object_storage.health.frappe.log_error"),
			patch("cargo.telemetry.datum.get_metrics_info", return_value=METRICS_INFO),
			patch("cargo.telemetry.datum.requests.post", return_value=MagicMock()) as post,
		):
			refresh_health()

		samples = {
			sample["labels"]["cluster"]: sample["labels"]["health"]
			for sample in post.call_args.kwargs["json"]["samples"]
			if sample["metric"] == HEALTH
		}
		self.assertEqual(samples[self.cluster], "Unknown")
