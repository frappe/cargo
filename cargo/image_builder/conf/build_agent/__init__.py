import json
import os
import re
import signal
import subprocess
import urllib.error
import urllib.request
from base64 import b64encode
from pathlib import Path

WORK_DIRECTORY = Path("/var/lib/deploy-agent")
LOG_FILE = WORK_DIRECTORY / "build.log"
REPORT_SECONDS = 10
LOG_TAIL_BYTES = 20_000
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
COMMIT = re.compile(r"[0-9a-f]{40}")
# Buildah finds the Containerfile or Dockerfile itself; Central checked the commit has one.
BUILD_SCRIPT = """
rm -rf source
mkdir source
cd source
git init --quiet
git fetch --quiet --depth 1 "https://github.com/$REPOSITORY.git" "$COMMIT"
git checkout --quiet FETCH_HEAD
buildah build --layers --tag "app:$COMMIT" .
"""


def call(method: str, **payload) -> dict | None:
	request = urllib.request.Request(
		f"{os.environ['CENTRAL_URL'].rstrip('/')}/api/method/central.deploy.api.{method}",
		data=json.dumps(payload).encode(),
		headers={"Content-Type": "application/json", "X-Deploy-Agent-Token": os.environ["AGENT_CREDENTIAL"]},
		method="POST",
	)
	with urllib.request.urlopen(request, timeout=30) as response:
		return json.load(response).get("message")


def report(build: dict, status: str) -> dict:
	log = LOG_FILE.read_bytes()[-LOG_TAIL_BYTES:].decode(errors="replace") if LOG_FILE.exists() else ""
	return call("report_build", deploy_request=build["deploy_request"], status=status, log=log) or {}


def get_build_environment(build: dict) -> dict[str, str]:
	"""The token goes in an HTTP header, so it is never written to the clone's config or the log."""
	basic = b64encode(f"x-access-token:{build['token']}".encode()).decode()
	environment = {key: value for key, value in os.environ.items() if key != "AGENT_CREDENTIAL"}
	return environment | {
		"REPOSITORY": build["repository"],
		"COMMIT": build["commit"],
		"GIT_CONFIG_COUNT": "1",
		"GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
		"GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
	}


def run(build: dict) -> None:
	"""Build in its own process group, so a cancel stops git and Buildah with it."""
	if not (REPOSITORY.fullmatch(build["repository"]) and COMMIT.fullmatch(build["commit"])):
		LOG_FILE.write_text("Central sent an invalid repository or commit.\n")
		report(build, "Failed")
		return

	with LOG_FILE.open("wb") as log:
		process = subprocess.Popen(
			["bash", "-euo", "pipefail", "-c", BUILD_SCRIPT],
			cwd=WORK_DIRECTORY,
			env=get_build_environment(build),
			stdout=log,
			stderr=subprocess.STDOUT,
			start_new_session=True,
		)
		while True:
			try:
				code = process.wait(timeout=REPORT_SECONDS)
				break
			except subprocess.TimeoutExpired:
				pass
			try:
				cancel = report(build, "Running").get("cancel")
			except urllib.error.URLError:
				# Central is briefly unreachable; the build goes on and the next report tells it.
				continue
			if cancel:
				os.killpg(process.pid, signal.SIGTERM)
				process.wait()
				return

	report(build, "Succeeded" if code == 0 else "Failed")


def main() -> None:
	WORK_DIRECTORY.mkdir(parents=True, exist_ok=True)
	if build := call("claim_build"):
		run(build)


if __name__ == "__main__":
	main()
