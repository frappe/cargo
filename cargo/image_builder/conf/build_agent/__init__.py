import json
import os
import subprocess
import urllib.request
from dataclasses import asdict
from pathlib import Path

from .models import (
	SECRETS_DIRECTORY,
	UNIT_PREFIX,
	Build,
	BuildReport,
	BuildStatus,
	BuildUnit,
)

WORK_DIRECTORY = Path("/var/lib/deploy-agent")
BUILD_SCRIPT = Path(__file__).with_name("build.sh")
# The live app's image and the ones a rollback can go back to.
KEPT_IMAGES = 3
# Leaves room for the live app, so running out of memory kills only the build.
BUILD_MEMORY_MAX = "80%"
STOP_TIMEOUT_SECONDS = 30
LOG_TAIL_LINES = 400
LOG_TAIL_BYTES = 20_000


def call(method: str, **payload) -> dict | None:
	request = urllib.request.Request(
		f"{os.environ['CENTRAL_URL'].rstrip('/')}/api/method/central.deploy.api.{method}",
		data=json.dumps(payload).encode(),
		headers={"Content-Type": "application/json", "X-Deploy-Agent-Token": os.environ["AGENT_CREDENTIAL"]},
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


def get_build_unit() -> BuildUnit | None:
	output = systemctl("list-units", "--all", "--plain", "--no-legend", f"{UNIT_PREFIX}*.service")
	names = [line.split()[0] for line in output.splitlines() if line.strip()]
	if len(names) > 1:
		raise RuntimeError(f"Only one build runs at a time, found {', '.join(names)}.")
	if not names:
		return None

	properties = dict(
		line.split("=", 1) for line in systemctl("show", names[0], "--property=SubState,Result").splitlines()
	)
	return BuildUnit(names[0], properties["SubState"], properties["Result"])


def get_build_log(unit: BuildUnit) -> str:
	log = subprocess.run(
		["journalctl", "--unit", unit.name, "--output", "cat", "--lines", str(LOG_TAIL_LINES), "--no-pager"],
		capture_output=True,
		text=True,
	).stdout
	if unit.is_out_of_memory:
		log += (
			f"\nBuild ran out of memory ({BUILD_MEMORY_MAX} of the VM's memory). "
			"Your live app is unaffected. Use a larger VM and push again.\n"
		)
	return log[-LOG_TAIL_BYTES:]


def check_build(unit: BuildUnit) -> None:
	"""Report the build to Central; stop it once Central cancels it, or clean up once it finished."""
	reply = call("report_build", **asdict(BuildReport(unit.deploy_request, unit.status, get_build_log(unit))))
	# `cancel` means a newer push replaced the build.
	if unit.is_finished or reply["cancel"]:
		remove_build(unit)


def remove_build(unit: BuildUnit) -> None:
	"""Stop kills the unit's whole cgroup, with SIGKILL after the stop timeout."""
	subprocess.run(["systemctl", "stop", unit.name], check=True)
	# A unit that succeeded is already unloaded after the stop, so this one may fail.
	subprocess.run(["systemctl", "reset-failed", unit.name], capture_output=True)
	unit.environment_file.unlink(missing_ok=True)
	prune_images()


def prune_images() -> None:
	"""Keep the newest app images; remove older ones, then the layers no image uses."""
	output = subprocess.run(
		[
			"buildah",
			"images",
			"--noheading",
			"--no-trunc",
			"--format",
			"{{.CreatedAtRaw}}\t{{.ID}}\t{{.Name}}",
		],
		check=True,
		capture_output=True,
		text=True,
	).stdout
	# CreatedAtRaw sorts as text because the VM's clock is UTC; one image tagged twice is listed once.
	images = sorted(
		{tuple(line.split("\t")) for line in output.splitlines() if line.endswith("/app")}, reverse=True
	)
	if old := [image_id for _created, image_id, _name in images[KEPT_IMAGES:]]:
		subprocess.run(["buildah", "rmi", *old], check=True)
	subprocess.run(["buildah", "rmi", "--prune"], check=True)


def start_build(build: Build) -> None:
	"""Systemd runs the build, so it outlives this tick and a restart of the agent."""
	SECRETS_DIRECTORY.mkdir(mode=0o700, exist_ok=True)
	# Root-only, so the token never shows in the unit's properties.
	with os.fdopen(
		os.open(build.environment_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w"
	) as file:
		file.writelines(f"{key}={value}\n" for key, value in build.environment.items())

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


def main() -> None:
	"""One tick: report the build that is running, or start the next one."""
	WORK_DIRECTORY.mkdir(parents=True, exist_ok=True)
	if unit := get_build_unit():
		check_build(unit)
	elif build := get_build():
		start_build(build)
		# Central hands out this build until it hears it runs; a failed start is retried next tick.
		call("report_build", **asdict(BuildReport(build.deploy_request, BuildStatus.RUNNING, "")))
