from __future__ import annotations

import subprocess
import time
import tomllib
import typing
from pathlib import Path

import frappe
import jwt
from frappe import _

if typing.TYPE_CHECKING:
	from cargo.telemetry.doctype.datum_server.datum_server import DatumServer

RENEW_BEFORE_SECONDS = 24 * 60 * 60
RENEWAL_COOLDOWN_SECONDS = 10 * 60
RENEWAL_REQUESTED_KEY = "cargo:datum-token-renewal-requested"
SETUP_TIMEOUT_SECONDS = 120


class MetricsInfo(typing.TypedDict):
	"""The metrics endpoint and token for a cluster"""

	endpoint: str
	token: str


def get_metrics_info() -> MetricsInfo:
	"""Steal metrics endpoint and token from `common_config.toml`"""
	bench_path = Path(frappe.utils.get_bench_path())
	common_bench_config = bench_path.parent / "common_config.toml"

	parsed = tomllib.loads(common_bench_config.read_text()) if common_bench_config.exists() else {}
	telemetry = parsed.get("telemetry") or {}
	metrics_endpoint, metrics_token = telemetry.get("endpoint"), telemetry.get("token")

	if not metrics_endpoint or not metrics_token:
		request_datum_token()
		raise RuntimeError(f"Cannot find metrics endpoint or token in {common_bench_config}")

	if is_token_expiring(metrics_token):
		request_datum_token()

	return MetricsInfo(endpoint=metrics_endpoint, token=metrics_token)


def is_token_expiring(token: str) -> bool:
	"""True within a day of `exp`, past it, or when the token carries no readable `exp`."""
	try:
		expires_at = jwt.decode(token, options={"verify_signature": False}).get("exp")
	except jwt.PyJWTError:
		return True

	return not isinstance(expires_at, int | float) or expires_at - time.time() <= RENEW_BEFORE_SECONDS


def request_datum_token() -> None:
	"""Report telemetry to Central again, which answers an Available report with a new token."""
	from cargo.telemetry.doctype.datum_server.datum_server import WEBHOOK_NAME

	server: DatumServer = frappe.get_single("Datum Server")
	if server.status != "Active":
		return

	if frappe.cache.get_value(RENEWAL_REQUESTED_KEY):
		return

	webhook = frappe.get_doc("Webhook", WEBHOOK_NAME)
	if not webhook.enabled:
		return

	frappe.cache.set_value(RENEWAL_REQUESTED_KEY, 1, expires_in_sec=RENEWAL_COOLDOWN_SECONDS)
	# Enqueue webhook just sends the request directly!
	frappe.enqueue(
		"frappe.integrations.doctype.webhook.webhook.enqueue_webhook",
		doc=frappe.get_single("Datum Server"),
		webhook=webhook,
		enqueue_after_commit=True,
	)


def write_telemetry_credential(endpoint: str, token: str) -> None:
	"""Let pilot write `[telemetry]`: it owns common_config.toml, its lock, and Fluent Bit."""
	bench_path = Path(frappe.utils.get_bench_path())
	pilot = bench_path.parent.parent / "bin" / "pilot"
	command = [
		str(pilot),
		"-b",
		str(bench_path),
		"setup",
		"telemetry",
		"--endpoint",
		endpoint,
		"--token",
		token,
	]

	try:
		subprocess.run(command, check=True, capture_output=True, text=True, timeout=SETUP_TIMEOUT_SECONDS)
	except subprocess.CalledProcessError as error:
		frappe.throw(
			_("Pilot could not write the Datum token: {0}").format((error.stderr or error.stdout)[-500:])
		)
	except subprocess.TimeoutExpired:
		frappe.throw(_("Pilot did not finish writing the Datum token in time."))

	frappe.cache.delete_value(RENEWAL_REQUESTED_KEY)
