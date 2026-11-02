from __future__ import annotations

import tomllib
import typing
from pathlib import Path

import frappe


class MetricsInfo(typing.TypedDict):
	"""The Datum endpoint and token this Cargo host ships its own metrics with."""

	endpoint: str
	token: str


def get_metrics_info() -> MetricsInfo:
	"""Read the host's Datum credential from `common_config.toml`, which Pilot keeps rotated.
	Raises `RuntimeError` while Pilot has not written one yet."""
	common_bench_config = Path(frappe.utils.get_bench_path()).parent / "common_config.toml"

	parsed = tomllib.loads(common_bench_config.read_text()) if common_bench_config.exists() else {}
	telemetry = parsed.get("telemetry") or {}
	metrics_endpoint, metrics_token = telemetry.get("endpoint"), telemetry.get("token")

	if not metrics_endpoint or not metrics_token:
		raise RuntimeError(f"Cannot find metrics endpoint or token in {common_bench_config}")

	return MetricsInfo(endpoint=metrics_endpoint, token=metrics_token)
