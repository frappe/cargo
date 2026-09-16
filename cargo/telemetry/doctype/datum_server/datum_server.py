# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

import typing

import frappe
from frappe import _
from frappe.utils import cint

from cargo.cargo.doctype.machine.machine import DEAD_STATES
from cargo.client_models import TELEMETRY, NodeSpec
from cargo.proxy_client import ProxyClient, ProxyError
from cargo.ssh import OutputLog, run_over_ssh, script
from cargo.workflow_engine.doctype.press_workflow.decorators import flow, task
from cargo.workflow_engine.doctype.press_workflow.workflow_builder import WorkflowBuilder

if typing.TYPE_CHECKING:
	from frappe.integrations.doctype.webhook.webhook import Webhook

	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings

CONF = ("telemetry", "conf", "datum", "install.sh")
NGINX_CONF = ("telemetry", "conf", "nginx", "install.sh")
DATUM_PORT = 8000
TRUSTED_PROXIES = ("127.0.0.1", "::1", "fd00::/8")
TELEMETRY_SITE_NAMES = ("telemetry-svc", "telemetry-read-svc")
SETUP_TIMEOUT = 30 * 60
PUBLIC_KEY_FILE = "/home/frappe/datum/.dev/datum.pub"
# Fixed by datum's own ACL migration, which creates exactly these two.
DATUM_USER = "datum"
MAX_PORT = 65535
SECRET_LENGTH = 32
WEBHOOK_ENDPOINT = "/api/method/central.api.cargo_webhooks.telemetry_webhook"
REPORTED_STATUSES = ("Active", "Failed")


class DatumServer(WorkflowBuilder):
	"""One datum host: the machine it runs on, and what it was built with."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		auto_spawn: DF.Check
		base_image: DF.Data
		clickhouse_host: DF.Data
		clickhouse_password: DF.Password | None
		clickhouse_port: DF.Int
		default_password: DF.Password | None
		error: DF.SmallText | None
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

	def after_insert(self) -> None:
		"""Central hands pilots this host's URL to ship metrics and logs to, so it has to hear
		when the host comes up or goes down."""
		configure_telemetry_webhook(self)

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
		)
		self.machine = machine.name
		self.save()

		return self.machine

	@frappe.whitelist()
	def setup(self) -> None:
		"""Install datum on this host's machine, and route to it once it answers."""
		machine_status = frappe.db.get_value("Machine", self.machine, "status")
		if machine_status != "Running":
			frappe.throw(_("Machine must be running to set up datum."), frappe.ValidationError)

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
				self.mark("Failed", "datum did not install. See the Setup Log.")
				return False

		return True

	@task(queue="long", timeout=3 * SETUP_TIMEOUT)
	def configure_routing(self) -> bool:
		"""Put nginx on port 80 in front of datum and ClickHouse."""
		from cargo.cargo.doctype.machine.machine import Machine

		machine: Machine = frappe.get_doc("Machine", self.machine)
		with OutputLog(self, "setup_log", append=True) as log:
			try:
				run_over_ssh(
					machine.address,
					script(*NGINX_CONF, environment=self.nginx_environment()),
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
			"TRUSTED_PROXIES": " ".join(TRUSTED_PROXIES),
		}

	@property
	def wildcard_domain(self) -> str:
		domain = frappe.db.get_single_value("Cargo Settings", "wildcard_domain", cache=True)
		if not domain:
			frappe.throw(_("Wildcard Domain must be set in Cargo Settings."))

		return domain

	@property
	def proxy_domains(self) -> tuple[str, ...]:
		return tuple(f"{site_name}.{self.wildcard_domain}" for site_name in TELEMETRY_SITE_NAMES)

	@task
	def publish_proxy_routes(self) -> bool:
		"""Point this region's telemetry domain at the host."""
		try:
			client = ProxyClient.from_settings()
			machine_address = frappe.db.get_value("Machine", self.machine, "address")
			for domain in self.proxy_domains:
				client.map_domain(domain, machine_address)
		except (ProxyError, frappe.ValidationError) as error:
			self.mark("Failed", _("Proxy route setup failed: {0}").format(error))
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
		"""What this host's machine settling means for it. Its state is already recorded;
		`sync_pending_machines` calls this once it changes."""
		status = frappe.db.get_value("Machine", self.machine, "status")

		if status in DEAD_STATES:
			return self.mark("Failed")

	def mark(self, status: str, error: str | None = None) -> None:
		self.status = status
		self.error = error
		self.save()

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
			"DATUM_PORT": DATUM_PORT,
		}


def configure_telemetry_webhook(server: DatumServer) -> None:
	"""Point a Frappe Webhook at Central so this host reports its own status changes."""
	settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")
	if not settings.central_url:
		raise frappe.ValidationError(_("Central URL must be set in Cargo Settings to configure webhook."))

	secret = settings.get_password("central_webhook_secret", raise_exception=True)
	name = f"datum_server-{server.name}"
	webhook: Webhook = (
		frappe.get_doc("Webhook", name) if frappe.db.exists("Webhook", name) else frappe.new_doc("Webhook")
	)
	webhook.name = name
	# TODO: Once the proxy is laid out, this host's public URL (a domain) becomes its
	# service_endpoint -- the URL pilots ship metrics and logs to.
	webhook.update(
		{
			"webhook_doctype": server.doctype,
			"webhook_docevent": "on_update",
			"request_url": settings.central_url.rstrip("/") + WEBHOOK_ENDPOINT,
			"request_method": "POST",
			"request_structure": "JSON",
			"condition": f"doc.status in {REPORTED_STATUSES}",
			"webhook_json": frappe.as_json(
				{
					"region": settings.region,
					"region_id": settings.region_id,
					"service": "telemetry",
					"status": "{{ doc.status }}",
					"service_endpoint": "<TBD>",
				}
			),
			"enable_security": True,
			"webhook_secret": secret,
			"enabled": True,
		}
	)
	webhook.save(ignore_permissions=True)
