from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Iterator

import frappe
import requests
from frappe.utils import now_datetime
from frappe.utils.password import get_decrypted_password

from cargo.object_storage.clock import as_utc
from cargo.object_storage.doctype.bucket_usage_day import bucket_usage_day as outbox
from cargo.object_storage.usage import SETTINGS

REPORT_PATH = "/api/method/central.billing.api.metering.report"
SERVICE = "storage"
METER = "bytes_stored"
BATCH_SIZE = 1000
TIMEOUT_SECONDS = 30
UNCONFIGURED_KEY = "cargo-bucket-usage-report-unconfigured"


class CentralUnavailable(Exception):
	"""Central could not be reached or failed. The days wait for the next run."""


class ReportRefused(Exception):
	"""Central refused the whole report. The days wait, and someone has to look."""


def report_usage() -> None:
	"""Send Central every day it has not confirmed, in signed batches. Scheduled hourly."""
	target = report_target()
	if not target:
		return

	url, secret = target
	for rows in pending_batches():
		try:
			reply = post_report(url, secret, report_body(rows))
		except CentralUnavailable as error:
			frappe.logger("cargo.usage").warning(f"Bucket usage report not sent: {error}")
			return
		except ReportRefused as error:
			frappe.log_error(title="Central refused the bucket usage report", message=str(error))
			return

		settle(rows, reply)
		# Frees this batch's outbox rows before the next call, so samplers do not wait on them.
		frappe.db.commit()  # nosemgrep


def report_target() -> tuple[str, str] | None:
	"""Where to send and what to sign with, or None while paused or not enrolled."""
	if frappe.db.get_single_value(SETTINGS, "pause_usage_report"):
		return None

	central_url = frappe.db.get_single_value(SETTINGS, "central_url")
	secret = get_decrypted_password(SETTINGS, SETTINGS, "central_report_secret", raise_exception=False)
	if central_url and secret:
		return central_url.rstrip("/") + REPORT_PATH, secret

	if not frappe.cache.get_value(UNCONFIGURED_KEY):
		frappe.logger("cargo.usage").warning("Bucket usage is not reported: no Central URL or report secret")
		frappe.cache.set_value(UNCONFIGURED_KEY, 1, expires_in_sec=86400)
	return None


def pending_batches() -> Iterator[list[frappe._dict]]:
	"""Pending days in batches Central accepts, each batch after the last by name."""
	last = ""
	while rows := outbox.pending(last, BATCH_SIZE):
		yield rows
		last = rows[-1].name


def report_body(rows: list[frappe._dict]) -> bytes:
	"""The report as signed and sent: `as_of` is the newest sample in it, in UTC."""
	as_of = max(as_utc(row.sampled_at) for row in rows)
	lines = [
		{"subject": row.bucket_name, "meter": METER, "day": str(row.day), "value": int(row.peak_bytes)}
		for row in rows
	]
	report = {"as_of": as_of.strftime("%Y-%m-%dT%H:%M:%SZ"), "lines": lines}
	return json.dumps(report, separators=(",", ":")).encode()


def sign(secret: str, body: bytes) -> str:
	return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def post_report(url: str, secret: str, body: bytes) -> dict:
	headers = {
		"Content-Type": "application/json",
		"X-Central-Service": SERVICE,
		"X-Central-Signature": sign(secret, body),
	}
	try:
		response = requests.post(url, data=body, headers=headers, timeout=TIMEOUT_SECONDS)
	except requests.RequestException as error:
		raise CentralUnavailable(str(error)) from error

	if response.status_code >= 500:
		raise CentralUnavailable(f"Central answered {response.status_code}")
	if not response.ok:
		raise ReportRefused(f"Central answered {response.status_code}: {response.text[:500]}")

	reply = verdict(response)
	if reply is None:
		raise ReportRefused(f"Central answered without a verdict: {response.text[:500]}")
	return reply


def verdict(response: requests.Response) -> dict | None:
	"""Central's answer, or None when the body is not one."""
	try:
		body = response.json()
	except ValueError:
		return None

	reply = body.get("message") if isinstance(body, dict) else None
	if not isinstance(reply, dict) or "accepted" not in reply:
		return None

	rejected = reply.get("rejected")
	if not isinstance(rejected, list) or not all(isinstance(line, dict) for line in rejected):
		return None
	return reply


def settle(rows: list[frappe._dict], reply: dict) -> None:
	"""Confirm only the days Central took. A day any rejection could mean waits; it is refused
	only when Central names its bucket and day and says `retry` false."""
	rejections = Rejections(reply["rejected"])
	accepted, refused = [], {}
	for row in rows:
		if not rejections.hold(row):
			accepted.append(row.name)
		elif refusal := rejections.refusal(row):
			refused[row.name] = refusal.get("reason")

	now = now_datetime()
	outbox.mark_sent(rows, now)
	outbox.confirm(accepted, now)
	outbox.refuse(refused)


class Rejections:
	"""Central's rejections indexed once per call, by subject and then by day. An older Central
	names only the subject, so a rejection without a day means every day of it, and one without
	a subject means every row."""

	def __init__(self, rejected: list[dict]) -> None:
		self.unscoped = False
		self.by_subject: dict[str, dict[str | None, list[dict]]] = {}
		for line in rejected:
			self.add(line)

	def add(self, line: dict) -> None:
		subject, day = line.get("subject"), line.get("day")
		if subject is None:
			self.unscoped = True
		elif isinstance(subject, str) and (day is None or isinstance(day, str)):
			self.by_subject.setdefault(subject, {}).setdefault(day, []).append(line)

	def hold(self, row: frappe._dict) -> bool:
		"""Whether any rejection could mean this day."""
		days = self.by_subject.get(row.bucket_name, {})
		return self.unscoped or None in days or str(row.day) in days

	def refusal(self, row: frappe._dict) -> dict | None:
		"""The rejection that names this very day and says never to send it again, if any."""
		lines = self.by_subject.get(row.bucket_name, {}).get(str(row.day), [])
		return next((line for line in lines if line.get("retry") is False), None)
