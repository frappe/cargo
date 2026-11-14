from __future__ import annotations

import requests

from cargo.telemetry.credential import get_metrics_info

REQUEST_TIMEOUT_SECONDS = 30


def send_to_datum(path: str, payload: dict) -> None:
	"""Post to this host's Datum with the host's own credential. Raises `RuntimeError` while the host has no
	credential, and `requests.RequestException` when Datum is unreachable or refuses the write."""
	info = get_metrics_info()
	response = requests.post(
		info["endpoint"].rstrip("/") + path,
		headers={"Authorization": f"Bearer {info['token']}"},
		json=payload,
		timeout=REQUEST_TIMEOUT_SECONDS,
	)
	response.raise_for_status()
