# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from cargo.cargo.doctype.machine.machine import DEAD_STATES
from cargo.client_models import TELEMETRY, NodeSpec
from cargo.ssh import OutputLog, run_over_ssh, script

CONF = ("telemetry", "conf", "install.sh")
SETUP_TIMEOUT = 30 * 60
PUBLIC_KEY_FILE = "/home/frappe/datum/.dev/datum.pub"
# Fixed by datum's own ACL migration, which creates exactly these two.
DATUM_USER = "datum"
MAX_PORT = 65535
SECRET_LENGTH = 32


class DatumServer(Document):
	"""One datum host: the machine it runs on, and what it was built with."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		base_image: DF.Data
		clickhouse_host: DF.Data
		clickhouse_password: DF.Password | None
		clickhouse_port: DF.Int
		default_password: DF.Password | None
		insights_password: DF.Password | None
		machine: DF.Link | None
		oidc_issuer: DF.Data | None
		public_key: DF.Code | None
		repository: DF.Data
		setup_log: DF.Code | None
		status: DF.Literal["Draft", "Setting Up", "Active", "Failed"]
		timeout_seconds: DF.Int
		version: DF.Data
	# end: auto-generated types

	def validate(self) -> None:
		self.validate_token_verification()
		self.validate_connection()

	def validate_token_verification(self) -> None:
		"""One of the two, and a real one: whitespace is truthy, and a key datum cannot read
		is the 401-to-everything this check exists to prevent."""
		self.oidc_issuer = (self.oidc_issuer or "").strip().rstrip("/") or None
		self.public_key = (self.public_key or "").strip() or None

		if not (self.oidc_issuer or self.public_key):
			frappe.throw(
				_("Set an OIDC issuer or a public key, or datum will answer 401 to everything."),
				frappe.ValidationError,
			)

	def validate_connection(self) -> None:
		"""Both reach datum as strings it cannot argue with, so a nonsense value here is a
		host that starts and then cannot serve."""
		if not 1 <= cint(self.clickhouse_port) <= MAX_PORT:
			frappe.throw(_("ClickHouse port must be between 1 and {0}.").format(MAX_PORT))

		if cint(self.timeout_seconds) < 1:
			frappe.throw(_("Timeout must be at least a second."), frappe.ValidationError)

	def before_insert(self) -> None:
		"""Every ClickHouse password this host uses. Generated once and kept here, because
		the install writes them into ClickHouse itself -- nothing to keep in step later.

		Hex, so none can carry the `--` or quotes datum-migrate refuses."""
		if not self.clickhouse_password:
			self.clickhouse_password = frappe.generate_hash(length=SECRET_LENGTH)

		if not self.insights_password:
			self.insights_password = frappe.generate_hash(length=SECRET_LENGTH)

		if not self.default_password:
			self.default_password = frappe.generate_hash(length=SECRET_LENGTH)

	@frappe.whitelist()
	def create_telemetry_node(self, cpu: int, ram_gb: int, disk_gb: int) -> str:
		"""Add a telemetry node requesting from atlas."""
		from cargo.cargo.doctype.machine.machine import Machine

		if self.machine:
			frappe.throw(_("This host already has a machine."), frappe.ValidationError)

		machine = Machine.request(
			self,
			NodeSpec(role=TELEMETRY, cpu=cint(cpu), ram_gb=cint(ram_gb), disk_gb=cint(disk_gb)),
			base_image=self.base_image,
			title=f"{self.name} telemetry",
		)
		self.machine = machine.name
		self.save()

		return self.machine

	@frappe.whitelist()
	def setup(self) -> None:
		"""Install datum on the machine and start it. Streams to `setup_log` as it runs."""
		machine_status = frappe.db.get_value("Machine", self.machine, "status")
		if machine_status != "Running":
			frappe.throw(_("Machine must be running to set up datum."), frappe.ValidationError)

		self.mark("Setting Up")
		frappe.enqueue_doc(
			"Datum Server",
			self.name,
			method="setup_machine",
			queue="long",
			timeout=SETUP_TIMEOUT,
			enqueue_after_commit=True,
		)

	def sync_machines(self) -> None:
		"""What this host's machine settling means for it. Its state is already recorded;
		`sync_pending_machines` calls this once it changes."""
		status = frappe.db.get_value("Machine", self.machine, "status")

		if status in DEAD_STATES:
			return self.mark("Failed")

	def mark(self, status: str) -> None:
		self.status = status
		self.save()

	def setup_machine(self) -> None:
		"""Install datum on the machine and start it. Streams to `setup_log` as it runs."""
		from cargo.cargo.doctype.machine.machine import Machine

		machine: Machine = frappe.get_doc("Machine", self.machine)
		with OutputLog(self, "setup_log", append=True) as log:
			try:
				run_over_ssh(
					machine.ipv4_address,
					script(*CONF, environment=self.install_environment()),
					machine.get_password("ssh_private_key"),
					timeout=SETUP_TIMEOUT,
					on_output=log.write,
				)
			except Exception:
				frappe.log_error(
					title=f"{self.name} failed to set up",
					message=frappe.get_traceback(with_context=True),
				)
				return self.mark("Failed")

		self.mark("Active")

	def environment(self, key_file: str = PUBLIC_KEY_FILE) -> dict[str, str]:
		"""What datum runs on. `key_file` is where the PEM was written on the host."""
		variables = {
			"DATUM_CLICKHOUSE_HOST": self.clickhouse_host,
			"DATUM_CLICKHOUSE_PORT": str(self.clickhouse_port),
			"DATUM_CLICKHOUSE_USER": DATUM_USER,
			"DATUM_CLICKHOUSE_PASSWORD": self.get_password("clickhouse_password", raise_exception=False),
			"DATUM_INSIGHTS_PASSWORD": self.get_password("insights_password", raise_exception=False),
			"DATUM_DEFAULT_PASSWORD": self.get_password("default_password", raise_exception=False),
			"DATUM_TIMEOUT": str(self.timeout_seconds),
			"DATUM_OIDC_ISSUER": self.oidc_issuer,
			# This is for the fastapi server to read.
			"DATUM_JWT_PUBLIC_KEY_FILE": key_file if self.public_key else None,
			# The PEM itself, which the install writes to that path.
			"DATUM_JWT_PUBLIC_KEY": self.public_key,
		}

		return {name: value for name, value in variables.items() if value}

	def install_environment(self, key_file: str = PUBLIC_KEY_FILE) -> dict[str, str]:
		"""What the install script needs on top of datum's own: which datum to install."""
		return {
			**self.environment(key_file),
			"DATUM_REPOSITORY": self.repository,
			"DATUM_VERSION": self.version,
		}
