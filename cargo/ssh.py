import os
import stat
import subprocess
import tempfile
import threading
import time
import typing
from collections.abc import Callable
from pathlib import Path

import frappe
from frappe import _

if typing.TYPE_CHECKING:
	from frappe.model.document import Document

SSH_TIMEOUT = 600
LOG_FLUSH_SECONDS = 3  # how often a running command's output is published
LOG_CACHE_TTL = 15 * 60
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
	"-o",
	"LogLevel=ERROR",
)


class SshError(RuntimeError):
	"""A command run over SSH failed."""


class OutputLog:
	"""Streams a command's output into a document field while it runs."""

	def __init__(
		self,
		document: "Document",
		fieldname: str,
		event: str = "ssh_output",
		flush_seconds: int = LOG_FLUSH_SECONDS,
		append: bool = False,
	) -> None:
		self.document = document
		self.fieldname = fieldname
		self.event = event
		self.flush_seconds = flush_seconds
		self.append = append
		self.lines: list[str] = []
		self.published = 0
		self.flushed_at = time.monotonic()

	def __enter__(self) -> "OutputLog":
		"""This run owns the field, unless it is adding to what an earlier one left."""
		if self.append:
			self.lines = [live_output(self.document, self.fieldname)]
			return self

		self.document.db_set(self.fieldname, None, update_modified=False)
		frappe.cache.delete_value(self.cache_key)

		return self

	def __exit__(self, *exception: object) -> None:
		self.flush()
		self.store()

	def write(self, line: str) -> None:
		self.lines.append(line)
		if time.monotonic() - self.flushed_at >= self.flush_seconds:
			self.flush()

	@property
	def cache_key(self) -> str:
		return cache_key(self.document.doctype, self.document.name, self.fieldname)

	def flush(self) -> None:
		if len(self.lines) == self.published:
			return

		self.published = len(self.lines)
		self.flushed_at = time.monotonic()
		text = "".join(self.lines)
		frappe.cache.set_value(self.cache_key, text, expires_in_sec=LOG_CACHE_TTL)
		frappe.publish_realtime(
			self.event,
			{"name": self.document.name, "fieldname": self.fieldname, "value": text},
			doctype=self.document.doctype,
			docname=self.document.name,
		)

	def store(self) -> None:
		"""The run is over, so the document takes over from the cache."""
		text = "".join(self.lines)
		if not text:
			return

		self.document.db_set(self.fieldname, text, update_modified=False)
		frappe.cache.delete_value(self.cache_key)


def create_keypair(comment: str) -> tuple[str, str]:
	"""A fresh ed25519 keypair, as (public, private)."""
	with tempfile.TemporaryDirectory() as directory:
		path = Path(directory) / "key"
		subprocess.run(
			["ssh-keygen", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
			check=True,
			capture_output=True,
		)

		return path.with_suffix(".pub").read_text().strip(), path.read_text()


def cache_key(doctype: str, name: str, fieldname: str) -> str:
	return f"ssh_output:{doctype}:{name}:{fieldname}"


def live_output(document: "Document", fieldname: str) -> str:
	"""What a command has printed so far: the cache while it runs, the field once it ends."""
	return (
		frappe.cache.get_value(cache_key(document.doctype, document.name, fieldname))
		or document.get(fieldname)
		or ""
	)


@frappe.whitelist()
def get_live_output(doctype: str, name: str, fieldname: str) -> str:
	"""`live_output` for any document, so no doctype needs an endpoint of its own."""
	document = frappe.get_doc(doctype, name)
	document.check_permission("read")

	if not document.meta.has_field(fieldname):
		frappe.throw(_("{0} has no field {1}.").format(doctype, fieldname))

	return live_output(document, fieldname)


def run_over_ssh(
	address: str,
	script: str,
	key: str | None,
	user: str = "root",
	timeout: int = SSH_TIMEOUT,
	on_output: Callable[[str], None] | None = None,
) -> str:
	"""Pipe a script to ``bash -s`` and return what it printed, line by line as it arrives."""
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
