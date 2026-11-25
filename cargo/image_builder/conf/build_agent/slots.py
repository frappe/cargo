import os
from pathlib import Path

WORK_DIRECTORY = Path("/var/lib/deploy-agent")
SLOTS_DIRECTORY = WORK_DIRECTORY / "slots"
# The one record of the live slot, so a release stopped halfway is safe to run again.
UPSTREAM_FILE = WORK_DIRECTORY / "upstream.conf"
PORTS = {"blue": 8001, "green": 8002}
IMAGE_PREFIX = "localhost/app:"


def get_live_slot() -> str | None:
	"""The slot nginx sends traffic to; None before the first release."""
	if not UPSTREAM_FILE.exists():
		return None
	upstream = UPSTREAM_FILE.read_text()
	for slot, port in PORTS.items():
		if f"127.0.0.1:{port};" in upstream:
			return slot
	raise RuntimeError(f"{UPSTREAM_FILE} names no slot: {upstream!r}")


def get_slot_value(slot: str, key: str) -> str | None:
	"""A value the slot's last release wrote; None for a slot no release has used."""
	environment_file = SLOTS_DIRECTORY / f"{slot}.env"
	if not environment_file.exists():
		return None
	for line in environment_file.read_text().splitlines():
		if line.startswith(f"{key}="):
			return line.removeprefix(f"{key}=")
	return None


def write_slot(slot: str, commit: str, deploy_request: str, secrets: dict[str, str]) -> None:
	"""The image and port for app@<slot> and the Deploy Request the agent reports the app's health on, which systemd
	reads, and the app's secrets in a root-only file only podman reads. Podman takes each value as written; systemd
	would join the line after a secret ending in a backslash or an open quote."""
	SLOTS_DIRECTORY.mkdir(parents=True, exist_ok=True)
	(SLOTS_DIRECTORY / f"{slot}.env").write_text(
		f"IMAGE={IMAGE_PREFIX}{commit}\nPORT={PORTS[slot]}\nDEPLOY_REQUEST={deploy_request}\n"
	)
	with os.fdopen(
		os.open(SLOTS_DIRECTORY / f"{slot}.secrets.env", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w"
	) as file:
		file.writelines(f"{key}={value}\n" for key, value in secrets.items())


def switch_upstream(slot: str) -> None:
	"""The rename is atomic, so nginx never reads a half-written file."""
	staged = UPSTREAM_FILE.with_suffix(".new")
	staged.write_text(f"server 127.0.0.1:{PORTS[slot]};\n")
	staged.replace(UPSTREAM_FILE)
