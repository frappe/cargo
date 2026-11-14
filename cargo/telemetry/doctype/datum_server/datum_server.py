# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

import re
import typing
from functools import cached_property

import frappe
from frappe import _
from frappe.utils import cint

from cargo.atlas_client import AtlasClient, base_image_id
from cargo.auth import generate_machine_token
from cargo.caddy import require_public
from cargo.cargo.doctype.machine.machine import DEAD_MACHINE_STATES, Machine
from cargo.client_models import TELEMETRY, NodeSpec
from cargo.ssh import OutputLog, cache_key, run_over_ssh, script
from cargo.telemetry.doctype.datum_server.proxy import (
	DATUM_PORT,
	TELEMETRY_READ_SITE_NAME,
	TELEMETRY_WRITE_SITE_NAME,
	Proxy,
)
from cargo.telemetry.vector import datum_refresh_script, install_script
from cargo.workflow_engine.doctype.press_workflow.decorators import flow, task
from cargo.workflow_engine.doctype.press_workflow.workflow_builder import WorkflowBuilder

if typing.TYPE_CHECKING:
	from collections.abc import Callable

	from frappe.integrations.doctype.webhook.webhook import Webhook

	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings

SETUP_TIMEOUT = 30 * 60
MAX_PORT = 65535
CLICKHOUSE_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+\.\d+")
USER_PASSWORDS = (
	"datum_user_password",
	"insights_user_password",
	"beacon_user_password",
	"default_user_password",
)
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
		branch: DF.Data
		clickhouse_host: DF.Data | None
		clickhouse_port: DF.Int
		clickhouse_version: DF.Data
		commit: DF.Data
		beacon_user_password: DF.Password | None
		datum_user_password: DF.Password | None
		default_user_password: DF.Password | None
		error: DF.SmallText | None
		insights_user_password: DF.Password | None
		machine: DF.Link | None
		repository: DF.Data
		setup_log: DF.Code | None
		status: DF.Literal["Draft", "Setting Up", "Active", "Unreachable", "Failed", "Updating"]
		timeout_seconds: DF.Int
	# end: auto-generated types

	def before_validate(self) -> None:
		"""A Single saved before a field existed has no value, and `mark` saves on every
		status change, so the doctype default fills it rather than every save failing."""
		for fieldname in ("clickhouse_version", "branch"):
			value = (self.get(fieldname) or "").strip()
			self.set(fieldname, value or self.meta.get_field(fieldname).default)

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
			is_termination_protected=True,
			has_public_ipv4=True,
			is_privileged=True,
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
	def update_dns_records(self) -> None:
		"""Point the telemetry names at the datum host again."""
		self.check_permission("write")
		self.proxy.update_dns_records()

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
		"""Install datum on this host's machine, and route to it once it answers. Refused before
		anything changes when the host cannot take public traffic yet."""
		machine = frappe.get_doc("Machine", self.machine) if self.machine else None
		require_public(machine, _("datum host"))
		self.setup_log = None
		frappe.cache.delete_value(cache_key(self.doctype, self.name, "setup_log"))
		self.mark("Setting Up")
		self._setup.run_as_workflow()

	@task(queue="long", timeout=3 * SETUP_TIMEOUT)
	def start_setup_on_machine(self) -> bool:
		"""Install datum on the machine. Streams to `setup_log` as it runs."""
		machine: Machine = frappe.get_doc("Machine", self.machine)
		with OutputLog(self, "setup_log", append=True) as log:
			try:
				run_over_ssh(
					machine.address,
					script(
						"telemetry",
						"conf",
						"datum",
						"install.sh",
						environment=self.install_environment(commit=self.commit),
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

			# Nothing hands the host a token later, so a host without one is not set up.
			try:
				self.install_metrics_exporter(on_output=log.write)
			except Exception:
				frappe.log_error(
					title=f"Could not install the metrics exporter on {self.machine}",
					message=frappe.get_traceback(with_context=True),
				)
				self.mark("Failed", "The metrics exporter did not install. See the Setup Log.")
				return False

		return True

	def install_metrics_exporter(self, on_output: Callable[[str], None] | None = None) -> None:
		"""Install Vector on the host and the timer that fetches its own Datum token. Safe to repeat."""
		machine: Machine = frappe.get_doc("Machine", self.machine)
		settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")
		key = machine.get_password("ssh_private_key")

		# Atlas replaces the whole map, so the role set at creation is sent again.
		AtlasClient.from_settings().replace_vm_metadata(
			machine.vm_id,
			{"role": TELEMETRY, "datum_telemetry_token": generate_machine_token(TELEMETRY, machine.name)},
		)
		# Vector only installs once the host holds its own Datum token.
		run_over_ssh(machine.address, datum_refresh_script(), key, timeout=SETUP_TIMEOUT, on_output=on_output)
		run_over_ssh(
			machine.address,
			install_script(TELEMETRY, machine.name, settings.region),
			key,
			timeout=SETUP_TIMEOUT,
			on_output=on_output,
		)

	@frappe.whitelist()
	def update_datum(self, branch: str | None = None, commit: str | None = None) -> None:
		"""Move datum to `commit`, or to the newest commit of `branch`, and restart it."""
		if self.status not in ("Active", "Unreachable"):
			frappe.throw(_("Set datum up before updating it."), frappe.ValidationError)

		if frappe.db.get_value("Machine", self.machine, "status") != "Running":
			frappe.throw(_("Machine must be running to update datum."), frappe.ValidationError)

		self.setup_log = None
		frappe.cache.delete_value(cache_key(self.doctype, self.name, "setup_log"))
		self.mark("Updating")
		self._update.run_as_workflow((branch or "").strip() or self.branch, (commit or "").strip())

	@task(queue="long", timeout=3 * SETUP_TIMEOUT)
	def update_on_machine(self, branch: str, commit: str) -> bool:
		"""Pull datum to `commit`, or the newest of `branch`, and restart it. Streams to
		`setup_log` as it runs."""
		machine: Machine = frappe.get_doc("Machine", self.machine)
		with OutputLog(self, "setup_log", append=True) as log:
			try:
				run_over_ssh(
					machine.address,
					script(
						"telemetry",
						"conf",
						"datum",
						"update.sh",
						environment=self.install_environment(branch, commit),
					),
					machine.get_password("ssh_private_key"),
					timeout=SETUP_TIMEOUT,
					on_output=log.write,
				)
			except Exception:
				frappe.log_error(
					title=f"{self.name} failed to update to {commit or branch}",
					message=frappe.get_traceback(with_context=True),
				)
				return False

		return True

	@task
	def get_datum_commit(self) -> str:
		"""The commit datum runs at now."""
		machine: Machine = frappe.get_doc("Machine", self.machine)
		return run_over_ssh(
			machine.address,
			"su - frappe -c 'git -C datum rev-parse HEAD'",
			machine.get_password("ssh_private_key"),
		).strip()

	@flow
	def _update(self, branch: str, commit: str) -> None:
		"""Never Failed, which the spawner answers with a full setup. Unreachable leaves the
		health ping to find out whether datum still answers, and the operator to try again."""
		if not self.update_on_machine(branch, commit):
			return self.mark("Unreachable", _("Could not update datum. See the Setup Log."))

		self.branch = branch
		self.commit = self.get_datum_commit()
		self.mark("Active")

	def on_workflow_failure(self, workflow) -> None:
		"""A flow that raised would otherwise leave the host in Setting Up or Updating, which
		nothing watches. Failed setup is retried by the spawner; a failed update is not."""
		if self.status == "Setting Up":
			self.mark("Failed", _("Setup stopped before it finished. See the Setup Log."))
		elif self.status == "Updating":
			self.mark("Unreachable", _("The update stopped before it finished. See the Setup Log."))

	@cached_property
	def proxy(self) -> Proxy:
		return Proxy(self)

	@task(queue="long", timeout=3 * SETUP_TIMEOUT)
	def install_caddy(self) -> bool:
		"""Caddy on the host, in front of datum and ClickHouse. Run on every setup, so setting
		up again is also how a failed one is retried."""
		with OutputLog(self, "setup_log", append=True) as log:
			try:
				self.proxy.install(on_output=log.write)
			except Exception:
				frappe.log_error(
					title=f"{self.name} proxy setup failed",
					message=frappe.get_traceback(with_context=True),
				)
				self.mark("Failed", _("Caddy did not come up. See the Setup Log, then set up again."))
				return False

		return True

	@task
	def check_if_proxy_is_configured(self) -> bool:
		"""Whether the host answers on its public domains with a certificate Caddy obtained."""
		if self.proxy.wait_until_serving():
			return True

		public_ipv4 = frappe.db.get_value("Machine", self.machine, "public_ipv4")
		self.mark(
			"Failed",
			_(
				"{0} does not answer over HTTPS. Cargo pointed DNS for {1} at {2}. Set up again once it resolves."
			).format(f"https://{self.proxy_domains[0]}", ", ".join(self.proxy_domains), public_ipv4),
		)
		return False

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
		"""The URL pilots ship metrics and logs to. Plain HTTP over the WireGuard mesh, which
		Atlas routes for `internal-` names, so it never crosses the public internet."""
		return f"http://internal-{TELEMETRY_WRITE_SITE_NAME}.{self.wildcard_domain}"

	@flow
	def _setup(self) -> None:
		if not self.start_setup_on_machine():
			return

		self.commit = self.get_datum_commit()

		if not (self.install_caddy() and self.check_if_proxy_is_configured()):
			return

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
			"BEACON_USER_PASSWORD": self.get_password("beacon_user_password", raise_exception=False),
			"DEFAULT_USER_PASSWORD": self.get_password("default_user_password", raise_exception=False),
			"DATUM_TIMEOUT": str(self.timeout_seconds),
			"DATUM_JWKS_URL": settings.jwks_url,
			"DATUM_REGION_ID": str(settings.region_id),
		}

		return {name: value for name, value in variables.items() if value}

	def install_environment(self, branch: str | None = None, commit: str | None = None) -> dict[str, str]:
		"""What the install and update scripts need on top of datum's own: which datum to run.
		Without a commit they take the newest commit of the branch."""
		environment = {
			**self.environment(),
			"DATUM_REPOSITORY": self.repository,
			"DATUM_BRANCH": branch or self.branch,
			"DATUM_PORT": DATUM_PORT,
			"CLICKHOUSE_VERSION": self.clickhouse_version,
		}
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
