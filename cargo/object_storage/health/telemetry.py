from __future__ import annotations

import tomllib
import typing
from functools import cached_property
from pathlib import Path

import frappe
import requests

from cargo.atlas_client import host_port
from cargo.object_storage.garage import Garage

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)
	from cargo.object_storage.garage import MachineRow

INGEST_PATH = "/v1/ingest"
SCRAPE_TIMEOUT = 15
SHIP_TIMEOUT = 30
# Garage names some series `garage_*` and some not; `datum.samples` is shared with every
# other Frappe service, where a bare `cluster_healthy` would collide with anyone's.
PREFIX = "garage_"


class MetricsInfo(typing.TypedDict):
	"""The metrics endpoint and token for a cluster"""

	endpoint: str
	token: str


def get_metrics_info() -> MetricsInfo:
	"""Steal metrics endpoint and token from `common_config.toml`"""
	bench_path = Path(frappe.utils.get_bench_path())
	common_bench_config = bench_path.parent / "common_config.toml"

	if not common_bench_config.exists():
		raise RuntimeError(f"Cannot find {common_bench_config}")

	parsed_common_bench_config = tomllib.loads(common_bench_config.read_text())
	# Pilot writes the metrics destination as [datum]; [logs] is the same host, other path.
	datum = parsed_common_bench_config.get("datum") or {}
	metrics_endpoint, metrics_token = datum.get("endpoint"), datum.get("token")

	if not metrics_endpoint or not metrics_token:
		raise RuntimeError(f"Cannot find metrics endpoint or token in {common_bench_config}")

	return MetricsInfo(endpoint=metrics_endpoint, token=metrics_token)


def parse_metrics(text: str, labels: dict[str, str], timestamp: str) -> list[dict]:
	"""Prometheus text to datum samples.

	Histogram buckets are dropped: they are 1530 of the 1763 lines an idle node exports, and
	`datum.samples` has no TTL. `_sum` and `_count` survive, so averages still work. Anything
	unparseable is skipped rather than trusted -- stderr is merged into this stream."""
	samples = []
	for line in text.splitlines():
		line = line.strip()
		if not line or line.startswith("#"):
			continue

		name, _, rest = line.partition("{")
		if rest:
			series_labels, _, value = rest.partition("}")
			pairs = dict(_split_label(pair) for pair in _split_labels(series_labels))
		else:
			name, _, value = line.partition(" ")
			pairs = {}

		name = name.strip()
		if not name or name.endswith("_bucket"):
			continue

		try:
			numeric = float(value.strip().split()[0])
		except (ValueError, IndexError):
			continue

		samples.append(
			{
				"metric": name if name.startswith(PREFIX) else f"{PREFIX}{name}",
				"value": numeric,
				"ts": timestamp,
				"labels": {**pairs, **labels},
			}
		)

	return samples


def _split_labels(text: str) -> list[str]:
	"""Split on commas outside quotes: label values carry commas of their own."""
	parts, current, quoted = [], "", False
	for character in text:
		if character == '"':
			quoted = not quoted
		if character == "," and not quoted:
			parts.append(current)
			current = ""
			continue
		current += character

	if current:
		parts.append(current)

	return parts


def _split_label(pair: str) -> tuple[str, str]:
	key, _, value = pair.partition("=")
	return key.strip(), value.strip().strip('"')


class Telemetry:
	"""Get all possible telemetry from a clusters metric endpoint and ship it to datum"""

	def __init__(self, cluster: ObjectStorageCluster) -> None:
		self.cluster = cluster
		self.garage = Garage(self.cluster)

	@cached_property
	def metrics_token(self) -> str:
		return self.cluster.get_password("metrics_token")

	def check(self) -> None:
		"""Run the telemetry checks"""
		self.ship()

	def ship(self) -> None:
		"""Scrape every node and relay it. Each machine ships on its own: one unreachable
		node must not cost the cluster its whole tick."""
		info = get_metrics_info()
		timestamp = frappe.utils.now_datetime().isoformat()

		for machine in self.garage.machines:
			try:
				samples = self.samples_for(machine, timestamp)
			except Exception:
				frappe.log_error(title=f"Could not scrape {machine['name']}")
				continue

			if samples:
				self.send(info, samples, machine)

	def samples_for(self, machine: MachineRow, timestamp: str) -> list[dict]:
		"""One scrape of a node, labelled with where it came from. Garage's own labels
		(`volume`, `id`, `rpc_endpoint`) pass through untouched."""
		return parse_metrics(
			self.scrape(machine),
			labels={
				"cluster": self.cluster.name,
				"region": self.cluster.region,
				"role": machine["role"],
				"machine": machine["name"],
			},
			timestamp=timestamp,
		)

	def scrape(self, machine: MachineRow) -> str:
		"""Every node serves its own metrics on the admin port, reached over the mesh."""
		response = requests.get(
			f"http://{host_port(machine['address'], self.cluster.admin_port)}/metrics",
			headers={"Authorization": f"Bearer {self.metrics_token}"},
			timeout=SCRAPE_TIMEOUT,
		)
		response.raise_for_status()

		return response.text

	def send(self, info: MetricsInfo, samples: list[dict], machine: MachineRow) -> None:
		"""Datum drops writes by design rather than buffering, so nothing is retried here:
		a gap in a chart beats a stalled cron."""
		try:
			response = requests.post(
				f"{info['endpoint'].rstrip('/')}{INGEST_PATH}",
				headers={"Authorization": f"Bearer {info['token']}"},
				json={"samples": samples},
				timeout=SHIP_TIMEOUT,
			)
		except requests.RequestException as error:
			frappe.log_error(title=f"Could not ship {machine['name']} metrics", message=str(error))
			return

		if not response.ok:
			frappe.log_error(
				title=f"Datum refused {machine['name']} metrics ({response.status_code})",
				message=response.text[:500],
			)
