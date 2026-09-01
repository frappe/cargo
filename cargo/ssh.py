import os
import stat
import subprocess
import tempfile

import frappe
from frappe import _

SSH_TIMEOUT = 600
OPTIONS = (
	"-o",
	"IdentitiesOnly=yes",
	"-o",
	"StrictHostKeyChecking=accept-new",
	"-o",
	"UserKnownHostsFile=/dev/null",
	"-o",
	"ConnectTimeout=15",
)


class SshError(RuntimeError):
	"""A command run over SSH failed."""


def run_over_ssh(
	address: str, script: str, key: str | None, user: str = "root", timeout: int = SSH_TIMEOUT
) -> str:
	"""Pipe a script to ``bash -s`` on a machine and return its stdout."""
	if not key:
		frappe.throw(_("No SSH private key, so {0} cannot be reached.").format(address))

	with tempfile.NamedTemporaryFile("w", delete=False) as key_file:
		key_file.write(key if key.endswith("\n") else f"{key}\n")
		path = key_file.name
	os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

	try:
		result = subprocess.run(
			["ssh", "-i", path, *OPTIONS, f"{user}@{address}", "bash -s"],
			input=script,
			capture_output=True,
			text=True,
			timeout=timeout,
			check=False,
		)
	finally:
		os.unlink(path)

	if result.returncode != 0:
		raise SshError(f"{address}: {(result.stderr or result.stdout).strip()[:500]}")

	return result.stdout
