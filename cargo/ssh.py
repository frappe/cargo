import os
import stat
import subprocess
import tempfile
import threading
import time
import typing
from collections.abc import Callable

import frappe
from frappe import _

if typing.TYPE_CHECKING:
	from frappe.model.document import Document

SSH_TIMEOUT = 600
LOG_FLUSH_SECONDS = 3  # how often a running command's output reaches its document
ERROR_TAIL = 3000  # characters of output kept when a command fails
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


class OutputLog:
	"""Streams a command's output into a document field while it runs.

	Batched: one write per line would spend the run talking to the database. Uncommitted --
	the realtime event carries the text, and the job's own transaction persists the rest."""

	def __init__(
		self,
		document: "Document",
		fieldname: str,
		event: str = "ssh_output",
		flush_seconds: int = LOG_FLUSH_SECONDS,
	) -> None:
		self.document = document
		self.fieldname = fieldname
		self.event = event
		self.flush_seconds = flush_seconds
		self.lines: list[str] = []
		self.written = 0
		self.flushed_at = time.monotonic()

	def __enter__(self) -> "OutputLog":
		return self

	def __exit__(self, *exception: object) -> None:
		self.flush()

	def write(self, line: str) -> None:
		self.lines.append(line)
		if time.monotonic() - self.flushed_at >= self.flush_seconds:
			self.flush()

	def flush(self) -> None:
		if len(self.lines) == self.written:
			return

		self.written = len(self.lines)
		self.flushed_at = time.monotonic()
		text = "".join(self.lines)
		self.document.db_set(self.fieldname, text, update_modified=False)
		frappe.publish_realtime(
			self.event,
			{"name": self.document.name, "fieldname": self.fieldname, "value": text},
			doctype=self.document.doctype,
			docname=self.document.name,
		)


def run_over_ssh(
	address: str,
	script: str,
	key: str | None,
	user: str = "root",
	timeout: int = SSH_TIMEOUT,
	on_output: Callable[[str], None] | None = None,
) -> str:
	"""Pipe a script to ``bash -s`` and return everything it printed, line by line as it
	arrives. Both streams are merged: installers interleave progress across them."""
	if not key:
		frappe.throw(_("No SSH private key, so {0} cannot be reached.").format(address))

	with tempfile.NamedTemporaryFile("w", delete=False) as key_file:
		key_file.write(key if key.endswith("\n") else f"{key}\n")
		path = key_file.name
	os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

	process = subprocess.Popen(
		["ssh", "-i", path, *OPTIONS, f"{user}@{address}", "bash -s"],
		stdin=subprocess.PIPE,
		stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT,
		text=True,
		bufsize=1,
	)
	# Popen has no timeout of its own once we are streaming, so a watchdog enforces it.
	watchdog = threading.Timer(timeout, process.kill)
	watchdog.start()
	lines: list[str] = []

	try:
		process.stdin.write(script)
		process.stdin.close()
		for line in process.stdout:
			lines.append(line)
			if on_output:
				on_output(line)
		process.wait()
	finally:
		watchdog.cancel()
		os.unlink(path)

	output = "".join(lines)
	if process.returncode != 0:
		raise SshError(f"{address} exited {process.returncode}:\n...{output[-ERROR_TAIL:]}")

	return output
