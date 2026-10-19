from __future__ import annotations

from datetime import UTC, datetime, timedelta

import frappe
import requests
from frappe.utils import get_datetime, now_datetime

INGEST_PATH = "/v1/ingest"
SHIP_TIMEOUT = 30
PREFIX = "cargo_pilot_image_"
IN_PROGRESS_STATUSES = ("Provisioning", "Building", "Snapshotting")
# Failed builds stay listed this long, so a chart still shows yesterday's failure.
RECENT_FAILURES = timedelta(days=7)
BUILD_FIELDS = [
	"name",
	"image_type",
	"frappe_branch",
	"signup_app",
	"pilot_version",
	"status",
	"auto_retry_count",
	"manually_failed",
	"error",
	"built_at",
	"modified",
]


class Telemetry:
	"""Ships the state of Pilot image builds to datum. Hourly: a build takes tens of minutes,
	and what this answers is whether images keep coming, not how one is doing."""

	def __init__(self, datum_endpoint: str, datum_token: str) -> None:
		self.datum_endpoint = datum_endpoint
		self.datum_token = datum_token
		self.now = now_datetime()
		self.timestamp = datetime.now(UTC).isoformat()

	def get_latest_base_builds(self) -> list[dict]:
		"""The newest Base build of each Frappe branch, whatever its status."""
		return self.get_latest_builds("Base")

	def get_latest_site_builds(self) -> list[dict]:
		"""The newest Site build of each Frappe branch, whatever its status."""
		return self.get_latest_builds("Site")

	def get_latest_app_builds(self) -> list[dict]:
		"""The newest Apps build of each signup app on each Frappe branch, whatever its status."""
		return self.get_latest_builds("Apps")

	def get_latest_builds(self, image_type: str) -> list[dict]:
		"""The newest build of `image_type` per Frappe branch, and per signup app for Apps
		images. Builds come newest first, so the first one seen for a branch is its latest."""
		builds = frappe.get_all(
			"Pilot Image",
			filters={"image_type": image_type, "status": ("!=", "Retired")},
			fields=BUILD_FIELDS,
			order_by="creation desc",
		)

		latest_builds = []
		seen = set()
		for build in builds:
			key = (build.frappe_branch, build.signup_app)
			if key in seen:
				continue

			seen.add(key)
			latest_builds.append(build)

		return latest_builds

	def get_recent_failed_builds(self) -> list[dict]:
		"""Builds that failed in the last week, with how many automatic retries they used."""
		return frappe.get_all(
			"Pilot Image",
			filters={"status": "Failed", "modified": (">", self.now - RECENT_FAILURES)},
			fields=BUILD_FIELDS,
			order_by="modified desc",
		)

	def get_newest_completed_builds(self) -> list[dict]:
		"""When each type, branch and app last got a usable image."""
		return frappe.get_all(
			"Pilot Image",
			filters={"status": "Completed"},
			fields=["image_type", "frappe_branch", "signup_app", {"MAX": "built_at", "as": "built_at"}],
			group_by="image_type, frappe_branch, signup_app",
		)

	def get_samples(self) -> list[dict]:
		latest_builds = (
			self.get_latest_base_builds() + self.get_latest_site_builds() + self.get_latest_app_builds()
		)

		samples = []
		samples += self.get_latest_samples(latest_builds)
		samples += self.get_in_progress_samples(latest_builds)
		samples += self.get_newest_completed_samples()
		samples += self.get_failure_samples()
		return samples

	def get_latest_samples(self, latest_builds: list[dict]) -> list[dict]:
		"""One sample per latest build, labelled with its status."""
		samples = []
		for build in latest_builds:
			labels = self.get_build_labels(build)
			labels["status"] = build.status
			samples.append(self.make_sample("latest", 1, labels))

		return samples

	def get_in_progress_samples(self, latest_builds: list[dict]) -> list[dict]:
		"""How long each unfinished build has gone without changing state, so a stuck one
		stands out."""
		samples = []
		for build in latest_builds:
			if build.status not in IN_PROGRESS_STATUSES:
				continue

			labels = self.get_build_labels(build)
			labels["status"] = build.status
			samples.append(
				self.make_sample("in_progress_seconds", self.get_seconds_since(build.modified), labels)
			)

		return samples

	def get_newest_completed_samples(self) -> list[dict]:
		"""How old the newest usable image is. A growing age says building has stopped,
		which no failure ever reports."""
		samples = []
		for build in self.get_newest_completed_builds():
			if not build.built_at:
				continue

			labels = {
				"image_type": build.image_type,
				"frappe_branch": build.frappe_branch,
				"signup_app": build.signup_app or "",
			}
			samples.append(
				self.make_sample(
					"newest_completed_age_seconds", self.get_seconds_since(build.built_at), labels
				)
			)

		return samples

	def get_failure_samples(self) -> list[dict]:
		"""One sample per recent failure, valued at its automatic retries. The error is the
		failed stage, then the traceback: its first line says where and its last line why."""
		samples = []
		for build in self.get_recent_failed_builds():
			error_lines = [line for line in (build.error or "").splitlines() if line.strip()]
			stage = error_lines[0] if error_lines else ""
			reason = error_lines[-1] if error_lines else ""

			labels = self.get_build_labels(build)
			labels["stage"] = stage[:140]
			labels["reason"] = reason[:200]
			labels["manually_failed"] = "1" if build.manually_failed else "0"
			samples.append(self.make_sample("failed_attempts", build.auto_retry_count, labels))

		return samples

	def get_build_labels(self, build: dict) -> dict[str, str]:
		return {
			"image": build.name,
			"image_type": build.image_type,
			"frappe_branch": build.frappe_branch,
			"signup_app": build.signup_app or "",
			"pilot_version": build.pilot_version,
		}

	def make_sample(self, metric: str, value: float, labels: dict[str, str]) -> dict:
		return {
			"metric": PREFIX + metric,
			"value": value,
			"ts": self.timestamp,
			"labels": labels,
		}

	def get_seconds_since(self, moment) -> float:
		return (self.now - get_datetime(moment)).total_seconds()

	def send(self) -> None:
		"""Write the image building metrics to datum. Datum drops writes rather than
		buffering, so nothing is retried: the next hour sends the whole state again."""
		samples = self.get_samples()
		if not samples:
			return

		try:
			response = requests.post(
				self.datum_endpoint.rstrip("/") + INGEST_PATH,
				headers={"Authorization": f"Bearer {self.datum_token}"},
				json={"samples": samples},
				timeout=SHIP_TIMEOUT,
			)
		except requests.RequestException as error:
			frappe.log_error(title="Could not ship Pilot image metrics", message=str(error))
			return

		if not response.ok:
			frappe.log_error(
				title=f"Datum refused Pilot image metrics ({response.status_code})",
				message=response.text[:500],
			)
