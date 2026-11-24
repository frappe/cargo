import json
import os
import subprocess
import urllib.request
from dataclasses import asdict
from functools import cache
from pathlib import Path

from .models import (
	SECRETS_DIRECTORY,
	Build,
	BuildReport,
	BuildStatus,
	BuildUnit,
	Release,
	ReleaseReport,
	ReleaseStatus,
	ReleaseUnit,
	Unit,
)
from .slots import WORK_DIRECTORY, get_live_slot, get_slot_value

BUILD_SCRIPT = Path(__file__).with_name("build.sh")
METADATA_URL = "http://169.254.169.254/latest"
# Central sets this guest metadata key when it creates the VM.
METADATA_KEY = "deploy-agent"
# The newest image, which may still wait for its release; the live one is kept besides it.
KEPT_IMAGES = 1
# Leaves room for the live app, so running out of memory kills only the build.
BUILD_MEMORY_MAX = "80%"
STOP_TIMEOUT_SECONDS = 30
# Central appends each report's lines; a build that prints more than this between ticks sends its newest part.
MAX_REPORT_CHARACTERS = 256 * 1024
CURSOR_MARKER = "-- cursor: "


@cache
def get_agent() -> dict[str, str]:
	"""Where Central is and this app's credential, read once per tick from the guest metadata."""
	token_request = urllib.request.Request(
		f"{METADATA_URL}/api/token", headers={"X-metadata-token-ttl-seconds": "60"}, method="PUT"
	)
	with urllib.request.urlopen(token_request, timeout=5) as response:
		token = response.read().decode()
	request = urllib.request.Request(
		f"{METADATA_URL}/meta-data/attributes/{METADATA_KEY}", headers={"X-metadata-token": token}
	)
	with urllib.request.urlopen(request, timeout=5) as response:
		return json.load(response)


def call(method: str, **payload) -> dict | None:
	agent = get_agent()
	request = urllib.request.Request(
		f"{agent['central_url'].rstrip('/')}/api/method/central.deploy.api.{method}",
		data=json.dumps(payload).encode(),
		headers={"Content-Type": "application/json", "X-Deploy-Agent-Token": agent["credential"]},
		method="POST",
	)
	with urllib.request.urlopen(request, timeout=30) as response:
		return json.load(response).get("message")


def get_build() -> Build | None:
	"""An invalid build is reported as failed, then raised, since it means Central is broken."""
	payload = call("get_build")
	if payload is None:
		return None
	try:
		return Build(**payload)
	except ValueError as error:
		call(
			"report_build", **asdict(BuildReport(payload["deploy_request"], BuildStatus.FAILED, f"{error}\n"))
		)
		raise


def systemctl(*arguments: str) -> str:
	return subprocess.run(["systemctl", *arguments], check=True, capture_output=True, text=True).stdout


def get_unit(unit_class: type[Unit]) -> Unit | None:
	"""The agent's build or release unit; one of each runs at a time."""
	output = systemctl("list-units", "--all", "--plain", "--no-legend", f"{unit_class.PREFIX}*.service")
	names = [line.split()[0] for line in output.splitlines() if line.strip()]
	if len(names) > 1:
		raise RuntimeError(f"Only one runs at a time, found {', '.join(names)}.")
	if not names:
		return None

	properties = dict(
		line.split("=", 1) for line in systemctl("show", names[0], "--property=SubState,Result").splitlines()
	)
	return unit_class(names[0], properties["SubState"], properties["Result"])


def write_environment(path: Path, environment: dict[str, str]) -> None:
	"""Root-only, so a token never shows in a unit's properties."""
	SECRETS_DIRECTORY.mkdir(mode=0o700, exist_ok=True)
	with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
		file.writelines(f"{key}={value}\n" for key, value in environment.items())


def read_environment(path: Path) -> dict[str, str]:
	return dict(line.split("=", 1) for line in path.read_text().splitlines() if line)


def get_new_log(unit: Unit) -> tuple[str, str | None]:
	"""The unit's journal lines since the last report Central accepted, and the cursor after them."""
	after = ["--after-cursor", unit.cursor_file.read_text()] if unit.cursor_file.exists() else []
	output = subprocess.run(
		[
			"journalctl",
			"--unit",
			unit.name,
			"--output",
			"cat",
			"--show-cursor",
			"--quiet",
			"--no-pager",
			*after,
		],
		capture_output=True,
		text=True,
	).stdout
	log, marker, cursor = output.rpartition(CURSOR_MARKER)
	if not marker:
		return "", None
	if len(log) > MAX_REPORT_CHARACTERS:
		log = "... earlier output skipped ...\n" + log[-MAX_REPORT_CHARACTERS:]
	return log, cursor.strip()


def save_cursor(unit: Unit, cursor: str | None) -> None:
	"""Called after Central accepted the lines, so a failed report sends them again next tick."""
	if cursor:
		unit.cursor_file.write_text(cursor)


def check_build(unit: BuildUnit) -> Release | None:
	"""Report the build to Central and clean up once it finished or Central cancelled it.
	Returns the release to start once the image is built."""
	log, cursor = get_new_log(unit)
	if unit.is_out_of_memory:
		log += (
			f"\nBuild ran out of memory ({BUILD_MEMORY_MAX} of the VM's memory). "
			"Your live app is unaffected. Use a larger VM and push again.\n"
		)
	reply = call("report_build", **asdict(BuildReport(unit.deploy_request, unit.status, log)))
	save_cursor(unit, cursor)
	release = None
	# `cancel` means a newer push replaced the build.
	if unit.status == BuildStatus.SUCCEEDED and not reply["cancel"]:
		# Read before removing the build deletes its environment file.
		release = Release.from_environment(read_environment(unit.environment_file))
	if unit.is_finished or reply["cancel"]:
		remove_unit(unit)
	return release


def check_release(unit: ReleaseUnit) -> None:
	"""Report the release to Central and clean up once it finished. Nothing cancels a release."""
	log, cursor = get_new_log(unit)
	call("report_deployment_status", **asdict(ReleaseReport(unit.deploy_request, unit.status, log)))
	save_cursor(unit, cursor)
	if unit.is_finished:
		remove_unit(unit)


def remove_unit(unit: Unit) -> None:
	"""Stop kills the unit's whole cgroup, with SIGKILL after the stop timeout."""
	subprocess.run(["systemctl", "stop", unit.name], check=True)
	# A unit that succeeded is already unloaded after the stop, so this one may fail.
	subprocess.run(["systemctl", "reset-failed", unit.name], capture_output=True)
	unit.environment_file.unlink(missing_ok=True)
	unit.cursor_file.unlink(missing_ok=True)
	prune_images()


def prune_images() -> None:
	"""Keep the newest app image and the live one; remove the others, then the layers no image uses."""
	live = get_live_slot()
	in_use = {get_slot_value(live, "IMAGE")} if live else set()
	output = subprocess.run(
		[
			"buildah",
			"images",
			"--noheading",
			"--no-trunc",
			"--format",
			"{{.CreatedAtRaw}}\t{{.ID}}\t{{.Name}}:{{.Tag}}",
		],
		check=True,
		capture_output=True,
		text=True,
	).stdout
	# CreatedAtRaw sorts as text because the VM's clock is UTC; one image tagged twice is listed once.
	images = sorted(
		{tuple(line.split("\t")) for line in output.splitlines() if "/app:" in line}, reverse=True
	)
	if old := [image_id for _created, image_id, name in images[KEPT_IMAGES:] if name not in in_use]:
		subprocess.run(["buildah", "rmi", *old], check=True)
	subprocess.run(["buildah", "rmi", "--prune"], check=True)


def start_build(build: Build) -> None:
	"""Systemd runs the build, so it outlives this tick and a restart of the agent."""
	write_environment(build.environment_file, build.environment)
	subprocess.run(
		[
			"systemd-run",
			f"--unit={build.unit}",
			f"--working-directory={WORK_DIRECTORY}",
			# Keeps a successful unit loaded until the next tick reports it.
			"--property=RemainAfterExit=yes",
			f"--property=MemoryMax={BUILD_MEMORY_MAX}",
			# Swap would slow the live app instead of killing the build.
			"--property=MemorySwapMax=0",
			f"--property=TimeoutStopSec={STOP_TIMEOUT_SECONDS}",
			f"--property=EnvironmentFile={build.environment_file}",
			"/bin/bash",
			str(BUILD_SCRIPT),
		],
		check=True,
	)


def start_release(release: Release) -> None:
	"""Systemd runs the release in its own unit, so its result and log stay apart from the build's."""
	write_environment(release.environment_file, release.environment)
	subprocess.run(
		[
			"systemd-run",
			f"--unit={release.unit}",
			f"--working-directory={WORK_DIRECTORY}",
			"--property=RemainAfterExit=yes",
			f"--property=TimeoutStopSec={STOP_TIMEOUT_SECONDS}",
			f"--setenv=PYTHONPATH={Path(__file__).parent.parent}",
			f"--property=EnvironmentFile={release.environment_file}",
			"/usr/bin/python3",
			# Keeps the working directory off the import path.
			"-P",
			"-m",
			"build_agent.release",
		],
		check=True,
	)


def main() -> None:
	"""One tick: report the running release or build, or start the next build."""
	WORK_DIRECTORY.mkdir(parents=True, exist_ok=True)
	if unit := get_unit(ReleaseUnit):
		check_release(unit)

	elif unit := get_unit(BuildUnit):
		if release := check_build(unit):
			start_release(release)
			# Like a build, Central hears the release runs only once its unit exists.
			call(
				"report_deployment_status",
				**asdict(ReleaseReport(release.deploy_request, ReleaseStatus.RELEASING, "")),
			)

	elif build := get_build():
		start_build(build)
		# Central hands out this build until it hears it runs; a failed start is retried next tick.
		call("report_build", **asdict(BuildReport(build.deploy_request, BuildStatus.RUNNING, "")))
