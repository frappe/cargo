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

from cargo.auth import generate_machine_token

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings

# Renewal runs hourly against a 7-day token, so two days leaves room for Central to be down.
RENEW_BEFORE_SECONDS = 2 * 24 * 60 * 60


class MetricsInfo(typing.TypedDict):
	"""The Datum endpoint and token this Cargo host ships its own metrics with."""

	endpoint: str
	token: str


def get_metrics_info() -> MetricsInfo:
	"""Read the host's Datum credential from `common_config.toml`. Raises `RuntimeError` while
	`renew_datum_token` has not written one yet."""
	common_bench_config = Path(frappe.utils.get_bench_path()).parent / "common_config.toml"

	parsed = tomllib.loads(common_bench_config.read_text()) if common_bench_config.exists() else {}
	telemetry = parsed.get("telemetry") or {}
	metrics_endpoint, metrics_token = telemetry.get("endpoint"), telemetry.get("token")

	if not metrics_endpoint or not metrics_token:
		raise RuntimeError(f"Cannot find metrics endpoint or token in {common_bench_config}")

	return MetricsInfo(endpoint=metrics_endpoint, token=metrics_token)


def renew_datum_token() -> None:
	"""Give this Cargo host a new Datum token when its own is missing or expiring. Its Pilot
	has no Central credential, so Cargo asks Central itself. Scheduled hourly in `hooks.py`."""
	server = frappe.get_single("Datum Server")
	if server.status != "Active":
		return

	try:
		token = get_metrics_info()["token"]
	except RuntimeError:
		token = None

	if token and not is_token_expiring(token):
		return

	write_telemetry_credential(server.service_endpoint, get_datum_token())


def is_token_expiring(token: str) -> bool:
	"""True near `exp`, past it, or when the token carries no readable `exp`."""
	try:
		expires_at = jwt.decode(token, options={"verify_signature": False}).get("exp")
	except jwt.PyJWTError:
		return True

	return not isinstance(expires_at, int | float) or expires_at - time.time() <= RENEW_BEFORE_SECONDS


def get_datum_token() -> str:
	"""Trade a machine token this Cargo signs for its own host at Central for a Datum token.
	One Cargo runs per region, so `cargo:<region>-cargo-server` names this host alone."""
	settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")
	response = requests.get(
		f"{settings.central_url.rstrip('/')}/api/method/central.api.cargo.datum_token",
		headers={
			"X-Cargo-Machine-Token": generate_machine_token("cargo", f"{settings.region}-cargo-server"),
			"Accept": "application/json",
		},
		timeout=(5, 30),
	)
	response.raise_for_status()

	return response.json()["message"]["token"]


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
		subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
	except subprocess.CalledProcessError as error:
		frappe.throw(
			_("Pilot could not write the Datum token: {0}").format((error.stderr or error.stdout)[-500:])
		)
	except subprocess.TimeoutExpired:
		frappe.throw(_("Pilot did not finish writing the Datum token in time."))
