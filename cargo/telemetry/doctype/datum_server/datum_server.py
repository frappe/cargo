# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

import re
import typing

import frappe
from frappe import _
from frappe.utils import cint

from cargo.atlas_client import base_image_id
from cargo.cargo.doctype.machine.machine import DEAD_MACHINE_STATES
from cargo.client_models import TELEMETRY, NodeSpec
from cargo.proxy_client import ProxyClient, ProxyError
from cargo.ssh import OutputLog, cache_key, run_over_ssh, script
from cargo.workflow_engine.doctype.press_workflow.decorators import flow, task
from cargo.workflow_engine.doctype.press_workflow.workflow_builder import WorkflowBuilder

if typing.TYPE_CHECKING:
	from frappe.integrations.doctype.webhook.webhook import Webhook

	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings

DATUM_PORT = 8000
TELEMETRY_WRITE_SITE_NAME = "telemetry-svc"
TELEMETRY_READ_SITE_NAME = "telemetry-read-svc"
SETUP_TIMEOUT = 30 * 60
MAX_PORT = 65535
CLICKHOUSE_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+\.\d+")
USER_PASSWORDS = ("datum_user_password", "insights_user_password", "default_user_password")
WEBHOOK_NAME = "datum_server"
SOURCE_HEADER = "X-FC-Source"
REGION_HEADER = "X-FC-Region"
SOURCE = "cargo"


class DatumServer(WorkflowBuilder):
	"""One datum host: the machine it runs on, and what it was built with."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		auto_setup_attempts: DF.Int
		auto_spawn: DF.Check
		clickhouse_host: DF.Data | None
		clickhouse_port: DF.Int
		clickhouse_version: DF.Data
		datum_user_password: DF.Password | None
		default_user_password: DF.Password | None
		error: DF.SmallText | None
		insights_user_password: DF.Password | None
		machine: DF.Link | None
		repository: DF.Data
		setup_log: DF.Code | None
		status: DF.Literal["Draft", "Setting Up", "Active", "Unreachable", "Failed", "Updating"]
		timeout_seconds: DF.Int
		version: DF.Data
	# end: auto-generated types

	def before_validate(self) -> None:
		"""A Single saved before the field existed has no value, and `mark` saves on every
		status change, so the doctype default fills it rather than every save failing."""
		self.clickhouse_version = (self.clickhouse_version or "").strip() or self.meta.get_field(
			"clickhouse_version"
		).default

	def validate(self) -> None:
		self.validate_token_verification()
		self.validate_connection()

	def validate_token_verification(self) -> None:
		"""Datum checks tokens against the same merged key set Cargo does, for the same
		region, so both come from Cargo Settings rather than being restated here. Without
		either, datum answers 401 to everything."""
		settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")

		if not (settings.jwks_url or "").strip():
			frappe.throw(
				_("Set the JWKS URL in Cargo Settings, or datum will answer 401 to everything."),
				frappe.ValidationError,
			)

		if not settings.region_id:
			frappe.throw(
				_("Set the region ID in Cargo Settings, or datum will take another region's tokens."),
				frappe.ValidationError,
			)

	def validate_connection(self) -> None:
		"""Both reach datum as strings it cannot argue with, so a nonsense value here is a
		host that starts and then cannot serve."""
		if not 1 <= cint(self.clickhouse_port) <= MAX_PORT:
			frappe.throw(_("ClickHouse port must be between 1 and {0}.").format(MAX_PORT))

		if cint(self.timeout_seconds) < 1:
			frappe.throw(_("Timeout must be at least a second."), frappe.ValidationError)

		if not CLICKHOUSE_VERSION_PATTERN.fullmatch(self.clickhouse_version):
			frappe.throw(
				_("ClickHouse version must be a full package version, such as 26.3.52.9."),
				frappe.ValidationError,
			)

	def before_save(self) -> None:
		"""Passwords the install writes into ClickHouse. Hex, so none carries what
		datum-migrate refuses. Here and not in `before_insert`, which a Single never runs."""
		for field in USER_PASSWORDS:
			if not self.get(field):
				self.set(field, frappe.generate_hash(length=32))

	def on_update(self) -> None:
		"""Tell Central when the host settles. Install writes this Single blank, and an empty
		one is no host."""
		if not self.repository:
			return

		if not frappe.db.exists("Webhook", WEBHOOK_NAME):
			configure_telemetry_webhook(self)

	@frappe.whitelist()
	def create_telemetry_node(self, cpu_millicores: int, ram_gb: int, disk_gb: int) -> str:
		"""Add a telemetry node requesting from atlas."""
		from cargo.cargo.doctype.machine.machine import Machine

		if self.machine:
			frappe.throw(_("This host already has a machine."), frappe.ValidationError)

		machine = Machine.request(
			self,
			NodeSpec(
				role=TELEMETRY,
				cpu_millicores=cint(cpu_millicores),
				ram_gb=cint(ram_gb),
				disk_gb=cint(disk_gb),
			),
			base_image=base_image_id(),
		)
		self.machine = machine.name
		self.save()

		return self.machine

	@frappe.whitelist()
	def reset_auto_setup_attempts(self) -> None:
		"""Give the spawner its setup attempts back, so it tries again by itself."""
		self.check_permission("write")
		self.db_set("auto_setup_attempts", 0)

	@frappe.whitelist()
	def reveal_password(self, fieldname: str) -> str:
		"""One of the ClickHouse user passwords the install wrote."""
		frappe.only_for("System Manager")
		if fieldname not in USER_PASSWORDS:
			frappe.throw(
				_("{0} is not a ClickHouse user password.").format(fieldname), frappe.ValidationError
			)

		return self.get_password(fieldname)

	@frappe.whitelist()
	def setup(self) -> None:
		"""Install datum on this host's machine, and route to it once it answers."""
		machine_status = frappe.db.get_value("Machine", self.machine, "status")
		if machine_status != "Running":
			frappe.throw(_("Machine must be running to set up datum."), frappe.ValidationError)

		self.setup_log = None
		frappe.cache.delete_value(cache_key(self.doctype, self.name, "setup_log"))
		self.mark("Setting Up")
		self._setup.run_as_workflow()

	@task(queue="long", timeout=3 * SETUP_TIMEOUT)
	def start_setup_on_machine(self) -> bool:
		"""Install datum on the machine. Streams to `setup_log` as it runs."""
		from cargo.cargo.doctype.machine.machine import Machine

		machine: Machine = frappe.get_doc("Machine", self.machine)
		with OutputLog(self, "setup_log", append=True) as log:
			try:
				run_over_ssh(
					machine.address,
					script(
						"telemetry", "conf", "datum", "install.sh", environment=self.install_environment()
					),
					machine.get_password("ssh_private_key"),
					timeout=SETUP_TIMEOUT,
					on_output=log.write,
				)
			except Exception:
				frappe.log_error(
					title=f"{self.name} failed to set up",
					message=frappe.get_traceback(with_context=True),
				)
				self.mark("Failed", "datum did not install. See the Setup Log.")
				return False

		return True

	@frappe.whitelist()
	def update_datum(self, commit: str | None = None) -> None:
		"""Move datum to `commit`, or to the newest commit of its version, and restart it."""
		if self.status not in ("Active", "Unreachable"):
			frappe.throw(_("Set datum up before updating it."), frappe.ValidationError)

		if frappe.db.get_value("Machine", self.machine, "status") != "Running":
			frappe.throw(_("Machine must be running to update datum."), frappe.ValidationError)

		self.setup_log = None
		frappe.cache.delete_value(cache_key(self.doctype, self.name, "setup_log"))
		self.mark("Updating")
		self._update.run_as_workflow((commit or "").strip())

	@task(queue="long", timeout=3 * SETUP_TIMEOUT)
	def update_on_machine(self, commit: str) -> bool:
		"""Pull datum to `commit` and restart it. Streams to `setup_log` as it runs."""
		from cargo.cargo.doctype.machine.machine import Machine

		machine: Machine = frappe.get_doc("Machine", self.machine)
		with OutputLog(self, "setup_log", append=True) as log:
			try:
				run_over_ssh(
					machine.address,
					script(
						"telemetry", "conf", "datum", "update.sh", environment=self.update_environment(commit)
					),
					machine.get_password("ssh_private_key"),
					timeout=SETUP_TIMEOUT,
					on_output=log.write,
				)
			except Exception:
				frappe.log_error(
					title=f"{self.name} failed to update",
					message=frappe.get_traceback(with_context=True),
				)
				self.mark("Failed", "datum did not update. See the Setup Log.")
				return False

		return True

	@flow
	def _update(self, commit: str) -> None:
		if self.update_on_machine(commit):
			self.mark("Active")

	@task(queue="long", timeout=3 * SETUP_TIMEOUT)
	def configure_routing(self) -> bool:
		"""Put nginx on port 80 in front of datum and ClickHouse."""
		from cargo.cargo.doctype.machine.machine import Machine

		machine: Machine = frappe.get_doc("Machine", self.machine)
		with OutputLog(self, "setup_log", append=True) as log:
			try:
				run_over_ssh(
					machine.address,
					script("telemetry", "conf", "nginx", "install.sh", environment=self.nginx_environment()),
					machine.get_password("ssh_private_key"),
					timeout=SETUP_TIMEOUT,
					on_output=log.write,
				)
			except Exception:
				frappe.log_error(
					title=f"{self.name} could not be routed to",
					message=frappe.get_traceback(with_context=True),
				)
				self.mark("Failed", "nginx did not come up. See the Setup Log.")
				return False

		return True

	def nginx_environment(self) -> dict[str, str]:
		"""What the host needs to route its two subdomains."""
		return {
			"WILDCARD_DOMAIN": self.wildcard_domain,
			"DATUM_PORT": DATUM_PORT,
			"CLICKHOUSE_PORT": self.clickhouse_port,
			"TRUSTED_PROXIES": "127.0.0.1 ::1 fd00::/8",
		}

	@property
	def wildcard_domain(self) -> str:
		domain = frappe.db.get_single_value("Cargo Settings", "wildcard_domain", cache=True)
		if not domain:
			frappe.throw(_("Wildcard Domain must be set in Cargo Settings."))

		return domain

	@property
	def proxy_domains(self) -> tuple[str, ...]:
		return tuple(
			f"{site_name}.{self.wildcard_domain}"
			for site_name in (TELEMETRY_WRITE_SITE_NAME, TELEMETRY_READ_SITE_NAME)
		)

	@property
	def service_endpoint(self) -> str:
		"""The URL pilots ship metrics and logs to, served by nginx on this host."""
		return f"https://{TELEMETRY_WRITE_SITE_NAME}.{self.wildcard_domain}"

	@task
	def publish_proxy_routes(self) -> bool:
		"""Point this region's telemetry domain at the host."""
		try:
			client = ProxyClient.from_settings()
			machine_address = frappe.db.get_value("Machine", self.machine, "address")
			for domain in self.proxy_domains:
				client.map_domain(domain, machine_address)
		except (ProxyError, frappe.ValidationError) as error:
			self.mark("Failed", _("Proxy route setup failed: {0}").format(str(error)))
			return False

		return True

	@flow
	def _setup(self) -> None:
		if not self.start_setup_on_machine():
			return

		if not self.configure_routing():
			return

		if self.publish_proxy_routes():
			self.mark("Active")

	def sync_machines(self) -> None:
		"""What this host's machine changing status means for it. `sync_all_machines` calls
		this once it changes. A stopped machine shows up as Unreachable through the health ping."""
		status = frappe.db.get_value("Machine", self.machine, "status")

		if status in DEAD_MACHINE_STATES:
			return self.mark("Failed")

	def mark(self, status: str, error: str | None = None) -> None:
		self.status = status
		self.error = error
		self.save()

	def environment(self) -> dict[str, str]:
		"""What datum runs on. The key set and the region come from Cargo Settings, which
		already holds both for this host: a token is verified against that set and must be
		addressed to that region, or another region's pilot could write here."""
		settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")
		variables = {
			"DATUM_CLICKHOUSE_HOST": self.clickhouse_host,
			"DATUM_CLICKHOUSE_PORT": str(self.clickhouse_port),
			# Created by datum's own ACL migration.
			"DATUM_CLICKHOUSE_USER": "datum",
			"DATUM_USER_PASSWORD": self.get_password("datum_user_password", raise_exception=False),
			"INSIGHTS_USER_PASSWORD": self.get_password("insights_user_password", raise_exception=False),
			"DEFAULT_USER_PASSWORD": self.get_password("default_user_password", raise_exception=False),
			"DATUM_TIMEOUT": str(self.timeout_seconds),
			"DATUM_JWKS_URL": settings.jwks_url,
			"DATUM_REGION_ID": str(settings.region_id),
		}

		return {name: value for name, value in variables.items() if value}

	def install_environment(self) -> dict[str, str]:
		"""What the install script needs on top of datum's own: which datum to install."""
		return {
			**self.environment(),
			"DATUM_REPOSITORY": self.repository,
			"DATUM_VERSION": self.version,
			"DATUM_PORT": DATUM_PORT,
			"CLICKHOUSE_VERSION": self.clickhouse_version,
		}

	def update_environment(self, commit: str) -> dict[str, str]:
		"""The install's environment, plus the commit to move to. Blank takes the newest of `version`."""
		environment = self.install_environment()
		if commit:
			environment["DATUM_COMMIT"] = commit

		return environment


def configure_telemetry_webhook(server: DatumServer) -> None:
	"""Point a Frappe Webhook at Central so this host reports its own status changes."""
	settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")
	if not settings.central_webhook_url:
		raise frappe.ValidationError(
			_("Central has not enrolled this Cargo yet, so there is nowhere to report to.")
		)

	secret = settings.get_password("central_webhook_secret", raise_exception=True)
	name = WEBHOOK_NAME
	webhook: Webhook = (
		frappe.get_doc("Webhook", name) if frappe.db.exists("Webhook", name) else frappe.new_doc("Webhook")
	)
	webhook.name = name
	webhook.update(
		{
			"webhook_doctype": server.doctype,
			"webhook_docevent": "on_update",
			"request_url": settings.central_webhook_url,
			"request_method": "POST",
			"request_structure": "JSON",
			"condition": "doc.status in ('Active', 'Unreachable', 'Failed')",
			"webhook_json": frappe.as_json(
				{
					"region": settings.region,
					"region_id": settings.region_id,
					"service": "telemetry",
					"status": "{{ 'Available' if doc.status == 'Active' else 'Not Available' }}",
					"service_endpoint": server.service_endpoint,
				}
			),
			"webhook_headers": [
				{"key": SOURCE_HEADER, "value": SOURCE},
				{"key": REGION_HEADER, "value": settings.region},
			],
			"enable_security": True,
			"webhook_secret": secret,
			"enabled": settings.central_webhook_enabled,
		}
	)
	webhook.save(ignore_permissions=True)
