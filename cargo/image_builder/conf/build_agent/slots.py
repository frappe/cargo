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
	"""The image and port for app@<slot>, the Deploy Request the agent reports the app's health on, and the app's
	secrets, which app@.service passes to the container. Root-only, since it holds the secrets."""
	SLOTS_DIRECTORY.mkdir(parents=True, exist_ok=True)
	environment = {
		**secrets,
		"IMAGE": f"{IMAGE_PREFIX}{commit}",
		"PORT": PORTS[slot],
		"DEPLOY_REQUEST": deploy_request,
	}
	with os.fdopen(
		os.open(SLOTS_DIRECTORY / f"{slot}.env", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w"
	) as file:
		# A slot file an older agent wrote is world-readable, and O_CREAT keeps an existing file's mode.
		os.fchmod(file.fileno(), 0o600)
		file.writelines(f"{key}={value}\n" for key, value in environment.items())


def switch_upstream(slot: str) -> None:
	"""The rename is atomic, so nginx never reads a half-written file."""
	staged = UPSTREAM_FILE.with_suffix(".new")
	staged.write_text(f"server 127.0.0.1:{PORTS[slot]};\n")
	staged.replace(UPSTREAM_FILE)
