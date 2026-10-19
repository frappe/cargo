from datetime import timedelta
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from cargo.image_builder.doctype.pilot_image.pilot_image import PilotImage
from cargo.image_builder.health import relay as relay_module
from cargo.image_builder.health import ship_metrics
from cargo.image_builder.health.relay import Telemetry
from cargo.testing import use_test_settings

ENDPOINT, TOKEN = "https://datum.test/", "datum-jwt"


class IntegrationTestPilotImageTelemetry(IntegrationTestCase):
	"""What datum learns about Pilot image builds each hour."""

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("pilot_image_telemetry_test")
		self.addCleanup(frappe.db.rollback, save_point="pilot_image_telemetry_test")
		use_test_settings()

	def image(self, image_type: str = "Site", status: str = "Completed", **values) -> PilotImage:
		"""An image in `status`, without renting a build machine."""
		signup_app = "erpnext" if image_type == "Apps" else ""
		with patch.object(PilotImage, "after_insert"):
			image: PilotImage = frappe.get_doc(
				{
					"doctype": "Pilot Image",
					"pilot_version": f"v0.0.1-{frappe.generate_hash(length=6)}",
					"frappe_branch": "version-16",
					"frappe_commit": "a" * 40,
					"image_type": image_type,
					"signup_app": signup_app,
					"app_commit": f"{signup_app}-commit" if signup_app else "",
				}
			).insert()

		image.db_set({"status": status, **values})
		return image

	def samples(self, metric: str) -> list[dict]:
		return [
			sample
			for sample in Telemetry(ENDPOINT, TOKEN).get_samples()
			if sample["metric"] == f"cargo_pilot_image_{metric}"
		]

	def images_in(self, metric: str) -> set[str]:
		return {sample["labels"].get("image") for sample in self.samples(metric)}

	def test_only_the_newest_build_of_each_slot_is_latest(self):
		older = self.image("Base")
		newer = self.image("Base", status="Building")

		latest = self.images_in("latest")
		self.assertIn(newer.name, latest)
		self.assertNotIn(older.name, latest)

	def test_a_retired_build_is_never_latest(self):
		retired = self.image("Site", status="Retired")

		self.assertNotIn(retired.name, self.images_in("latest"))

	def test_apps_builds_are_latest_per_signup_app(self):
		apps = self.image("Apps", status="Failed")

		sample = next(
			sample for sample in self.samples("latest") if sample["labels"].get("image") == apps.name
		)
		self.assertEqual(sample["labels"]["signup_app"], "erpnext")
		self.assertEqual(sample["labels"]["status"], "Failed")

	def test_only_a_build_in_progress_reports_how_long_it_has_been_idle(self):
		building = self.image("Base", status="Snapshotting")
		frappe.db.set_value(
			"Pilot Image",
			building.name,
			"modified",
			now_datetime() - timedelta(hours=2),
			update_modified=False,
		)

		sample = next(
			sample
			for sample in self.samples("in_progress_seconds")
			if sample["labels"]["image"] == building.name
		)
		self.assertGreaterEqual(sample["value"], 2 * 3600 - 60)
		self.assertEqual(sample["labels"]["status"], "Snapshotting")

		self.image("Base")
		self.assertNotIn(building.name, self.images_in("in_progress_seconds"))

	def test_the_newest_completed_image_reports_its_age(self):
		self.image("Site", built_at=now_datetime() - timedelta(days=3))
		self.image("Site", built_at=now_datetime() - timedelta(hours=1))

		site = [
			sample
			for sample in self.samples("newest_completed_age_seconds")
			if sample["labels"]["image_type"] == "Site"
		]
		self.assertEqual(len(site), 1)
		self.assertLess(site[0]["value"], 2 * 3600)

	def test_a_failure_says_where_and_why_and_how_often(self):
		failed = self.image(
			"Apps",
			status="Failed",
			auto_retry_count=3,
			error="Run Provision Script\nTraceback (most recent call last):\n  ...\nSshError: bench build failed\n",
		)

		sample = next(
			sample for sample in self.samples("failed_attempts") if sample["labels"]["image"] == failed.name
		)
		self.assertEqual(sample["value"], 3)
		self.assertEqual(sample["labels"]["stage"], "Run Provision Script")
		self.assertEqual(sample["labels"]["reason"], "SshError: bench build failed")
		self.assertEqual(sample["labels"]["manually_failed"], "0")

	def test_a_failure_older_than_a_week_is_left_out(self):
		failed = self.image("Base", status="Failed", error="Build\nboom")
		frappe.db.set_value(
			"Pilot Image", failed.name, "modified", now_datetime() - timedelta(days=8), update_modified=False
		)

		self.assertNotIn(failed.name, self.images_in("failed_attempts"))

	def test_samples_are_posted_once_to_datum_with_its_token(self):
		self.image("Base")

		with patch.object(relay_module.requests, "post", return_value=MagicMock(ok=True)) as post:
			Telemetry(ENDPOINT, TOKEN).send()

		post.assert_called_once()
		self.assertEqual(post.call_args.args[0], "https://datum.test/v1/ingest")
		self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": f"Bearer {TOKEN}"})
		self.assertEqual(post.call_args.kwargs["timeout"], relay_module.SHIP_TIMEOUT)
		self.assertTrue(post.call_args.kwargs["json"]["samples"])

	def test_a_refused_or_unreachable_datum_is_logged_not_raised(self):
		self.image("Base")

		for outcome in (
			{"return_value": MagicMock(ok=False, status_code=429, text="Too many requests.")},
			{"side_effect": requests.ConnectionError("down")},
		):
			with (
				patch.object(relay_module.requests, "post", **outcome),
				patch.object(relay_module.frappe, "log_error") as log_error,
			):
				Telemetry(ENDPOINT, TOKEN).send()

			log_error.assert_called_once()

	def test_nothing_is_shipped_without_datum_credentials(self):
		with (
			patch("cargo.image_builder.health.get_metrics_info", side_effect=RuntimeError),
			patch.object(relay_module.requests, "post") as post,
		):
			ship_metrics()

		post.assert_not_called()
