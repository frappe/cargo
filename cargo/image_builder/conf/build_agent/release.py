import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .slots import PORTS, UPSTREAM_FILE, get_live_slot, switch_upstream, write_slot

CHECK_INTERVAL_SECONDS = 2
# Every new container must keep running this long after it first answers, like Fly's smoke check.
SETTLE_SECONDS = 10


class NoRedirect(urllib.request.HTTPRedirectHandler):
	"""A redirect is the answer; following it could leave the VM."""

	def redirect_request(self, *_):
		return None


OPENER = urllib.request.build_opener(NoRedirect)


def systemctl(*arguments: str) -> str:
	return subprocess.run(["systemctl", *arguments], check=True, capture_output=True, text=True).stdout


def get_status_code(port: int, path: str) -> int | None:
	"""None when nothing answers HTTP on the port."""
	try:
		with OPENER.open(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
			return response.status
	except urllib.error.HTTPError as error:
		return error.code
	except OSError:
		return None


def is_ready(port: int, health_path: str) -> bool:
	"""With a health path, it must return 2xx; without one, any HTTP answer on / will do."""
	code = get_status_code(port, health_path or "/")
	return code is not None and (not health_path or 200 <= code < 300)


def is_running_steadily(slot: str) -> bool:
	"""Active, and not restarted since this release started it."""
	properties = dict(
		line.split("=", 1)
		for line in systemctl("show", f"app@{slot}", "--property=ActiveState,NRestarts").splitlines()
	)
	return properties["ActiveState"] == "active" and properties["NRestarts"] == "0"


def get_failure(slot: str, health_path: str, timeout: int) -> str | None:
	"""Why the slot is not fit to serve, or None once it is."""
	deadline = time.monotonic() + timeout
	while not is_ready(PORTS[slot], health_path):
		if time.monotonic() >= deadline:
			if health_path:
				return f"GET {health_path} did not return 2xx within {timeout}s."
			return f"Nothing answered HTTP on $PORT ({PORTS[slot]}) within {timeout}s. The app must listen on $PORT."
		time.sleep(CHECK_INTERVAL_SECONDS)

	time.sleep(SETTLE_SECONDS)
	if not is_running_steadily(slot):
		return f"The app stopped or restarted within {SETTLE_SECONDS}s of answering."
	return None


def switch_to_slot(slot: str, commit: str, deploy_request: str) -> None:
	"""Start the commit's image in the slot; traffic stays on the live slot."""
	write_slot(slot, commit, deploy_request)
	systemctl("restart", f"app@{slot}")
	print(f"Started app:{commit} in app@{slot} on port {PORTS[slot]}.", flush=True)


def observe_failure_and_exit(slot: str, health_path: str, timeout: int) -> None:
	"""Stop the slot and exit 1 when it is not fit to serve."""
	if failure := get_failure(slot, health_path, timeout):
		print(f"{failure} The live app is unchanged.", flush=True)
		subprocess.run(
			["journalctl", "--unit", f"app@{slot}", "--lines", "50", "--no-pager", "--output", "cat"]
		)
		systemctl("stop", f"app@{slot}")
		sys.exit(1)


def rollback_upstream(live: str | None, slot: str) -> None:
	"""Put the live slot back in nginx, stop the new slot, and exit 1."""
	switch_upstream(live) if live else UPSTREAM_FILE.unlink()
	print("nginx did not reload, so traffic stays on the live app.", flush=True)
	subprocess.run(["journalctl", "--unit", "nginx", "--lines", "20", "--no-pager", "--output", "cat"])
	systemctl("disable", "--now", f"app@{slot}")
	sys.exit(1)


def main() -> None:
	"""Start the commit in the idle slot, move traffic to it once it is fit, then stop the old slot."""
	commit = os.environ["COMMIT"]
	deploy_request = os.environ["DEPLOY_REQUEST"]
	health_path = os.environ["HEALTH_PATH"]
	timeout = int(os.environ["HEALTH_TIMEOUT"])
	live = get_live_slot()
	idle = "green" if live == "blue" else "blue"

	# Switch slot
	switch_to_slot(idle, commit, deploy_request)

	# Observe failure and exit if required
	observe_failure_and_exit(idle, health_path, timeout)

	systemctl("enable", f"app@{idle}")
	switch_upstream(idle)
	try:
		systemctl("reload-or-restart", "nginx")
	except subprocess.CalledProcessError:
		# nginx still serves the old slot; a record naming the new one would make the next release restart it.
		rollback_upstream(live, idle)

	# Disable the older serving app once nginx has reloaded
	if live:
		systemctl("disable", "--now", f"app@{live}")

	print(f"app@{idle} serves app:{commit}.")


if __name__ == "__main__":
	main()
