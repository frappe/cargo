from __future__ import annotations

import subprocess
import time
import tomllib
import typing
from pathlib import Path

import frappe
import jwt
import requests
from frappe import _

from cargo.issuer import issue_datum_token

# Renewal runs hourly against a 7-day token, so two days leaves room for a failed renewal.
RENEW_BEFORE_SECONDS = 2 * 24 * 60 * 60
METADATA_URL = "http://169.254.169.254/latest"
APPLICATION_TOKEN_KEY = "cargo:datum_application_token"


class MetricsInfo(typing.TypedDict):
	"""A Datum endpoint and the token to send with."""

	endpoint: str
	token: str


def get_application_credential() -> MetricsInfo:
	"""The credential the Cargo site sends its own metrics and Error Logs with, as resource `cargo`.
	Raises `RuntimeError` while the region's Datum host is not Active."""
	server = frappe.get_single("Datum Server")
	if server.status != "Active":
		raise RuntimeError("The Datum host of this region is not Active")

	token = frappe.cache.get_value(APPLICATION_TOKEN_KEY)
	if not token or is_token_expiring(token):
		token = issue_datum_token("cargo")
		frappe.cache.set_value(APPLICATION_TOKEN_KEY, token)

	return MetricsInfo(endpoint=server.service_endpoint, token=token)


def get_metrics_info() -> MetricsInfo:
	"""Read the credential that Pilot ships the machine telemetry of this VM with, from `common_config.toml`.
	Raises `RuntimeError` while `renew_datum_token` has not written one yet."""
	common_bench_config = Path(frappe.utils.get_bench_path()).parent / "common_config.toml"

	parsed = tomllib.loads(common_bench_config.read_text()) if common_bench_config.exists() else {}
	telemetry = parsed.get("telemetry") or {}
	metrics_endpoint, metrics_token = telemetry.get("endpoint"), telemetry.get("token")

	if not metrics_endpoint or not metrics_token:
		raise RuntimeError(f"Cannot find metrics endpoint or token in {common_bench_config}")

	return MetricsInfo(endpoint=metrics_endpoint, token=metrics_token)


def renew_datum_token() -> None:
	"""Give the Pilot of this VM a new Datum token, as its Atlas Virtual Machine, when its own is
	missing or expiring. Scheduled hourly in `hooks.py`."""
	server = frappe.get_single("Datum Server")
	if server.status != "Active":
		return

	try:
		token = get_metrics_info()["token"]
	except RuntimeError:
		token = None

	if token and not is_token_expiring(token):
		return

	write_telemetry_credential(server.service_endpoint, issue_datum_token(get_virtual_machine_name()))


def is_token_expiring(token: str) -> bool:
	"""True near `exp`, past it, or when the token carries no readable `exp`."""
	try:
		expires_at = jwt.decode(token, options={"verify_signature": False}).get("exp")
	except jwt.PyJWTError:
		return True

	return not isinstance(expires_at, int | float) or expires_at - time.time() <= RENEW_BEFORE_SECONDS


def get_virtual_machine_name() -> str:
	"""The name of the Atlas Virtual Machine that runs this Cargo, from its instance metadata."""
	session = requests.put(
		f"{METADATA_URL}/api/token", headers={"X-metadata-token-ttl-seconds": "60"}, timeout=5
	)
	session.raise_for_status()
	response = requests.get(
		f"{METADATA_URL}/meta-data/instance-id", headers={"X-metadata-token": session.text}, timeout=5
	)
	response.raise_for_status()

	return response.text.strip()


def write_telemetry_credential(endpoint: str, token: str) -> None:
	"""Let pilot write `[telemetry]`: it owns common_config.toml and its lock."""
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
		subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
	except subprocess.CalledProcessError as error:
		frappe.throw(
			_("Pilot could not write the Datum token: {0}").format((error.stderr or error.stdout)[-500:])
		)
	except subprocess.TimeoutExpired:
		frappe.throw(_("Pilot did not finish writing the Datum token in time."))
