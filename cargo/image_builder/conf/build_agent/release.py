import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .slots import PORTS, get_live_slot, switch_upstream, write_slot

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
	if health_path:
		return code is not None and 200 <= code < 300
	return code is not None


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


def main() -> None:
	"""Start the commit in the idle slot, move traffic to it once it is fit, then stop the old slot."""
	commit = os.environ["COMMIT"]
	health_path = os.environ["HEALTH_PATH"]
	timeout = int(os.environ["HEALTH_TIMEOUT"])
	live = get_live_slot()
	idle = "green" if live == "blue" else "blue"

	write_slot(idle, commit, os.environ["DEPLOY_REQUEST"])
	systemctl("restart", f"app@{idle}")
	print(f"Started app:{commit} in app@{idle} on port {PORTS[idle]}.", flush=True)

	if failure := get_failure(idle, health_path, timeout):
		print(f"{failure} The live app is unchanged.", flush=True)
		subprocess.run(
			["journalctl", "--unit", f"app@{idle}", "--lines", "50", "--no-pager", "--output", "cat"]
		)
		systemctl("stop", f"app@{idle}")
		sys.exit(1)

	# This makes the call to switch to the newer blue/green version in the proxy
	switch_upstream(idle)
	systemctl("reload-or-restart", "nginx")
	systemctl("enable", f"app@{idle}")
	# Disable the older serving app once nginx has reloaded
	if live:
		systemctl("disable", f"app@{live}")
		systemctl("stop", f"app@{live}")

	print(f"app@{idle} serves app:{commit}.")


if __name__ == "__main__":
	main()
