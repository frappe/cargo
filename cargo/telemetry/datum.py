from __future__ import annotations

import requests

from cargo.telemetry.credential import get_application_credential

REQUEST_TIMEOUT_SECONDS = 30


def send_to_datum(path: str, payload: dict) -> None:
	"""Post to this region's Datum as resource `cargo`. Raises `RuntimeError` while the Datum host is not
	Active, and `requests.RequestException` when Datum is unreachable or refuses the write."""
	info = get_application_credential()
	response = requests.post(
		info["endpoint"].rstrip("/") + path,
		headers={"Authorization": f"Bearer {info['token']}"},
		json=payload,
		timeout=REQUEST_TIMEOUT_SECONDS,
	)
	response.raise_for_status()
