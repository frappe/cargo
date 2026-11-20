from __future__ import annotations

import time
import typing
from collections.abc import Callable
from pathlib import Path
from string import Template

import frappe
import requests
from cloudtoolkit.ssh.client import read_script
from frappe import _

from cargo.atlas_client import ANYWHERE, AtlasClient, AtlasError

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.machine.machine import Machine

INSTALL_SCRIPT = ("nginx", "install.sh")
CERTIFICATE_DIRECTORY = "/etc/nginx/tls"
PROBE_TIMEOUT_SECONDS = 10
PROBE_ATTEMPTS = 6
PROBE_INTERVAL_SECONDS = 10


class Nginx:
	"""nginx on a service's public machines, all of them in DNS. A subclass names the machines,
	its domains, the config template, what fills it in, and how to tell the service answers. nginx
	serves the certificate files under `CERTIFICATE_DIRECTORY`; it never obtains one itself."""

	name: str
	template: tuple[str, ...]

	@property
	def machines(self) -> list[Machine]:
		"""Every machine that serves the domains."""
		raise NotImplementedError

	@property
	def health_check_url(self) -> str | None:
		"""What Route53 asks each public address, so a machine that stops answering leaves DNS."""
		return None

	@property
	def domains(self) -> tuple[str, ...]:
		raise NotImplementedError

	def get_environment(self, machine: Machine) -> dict[str, str]:
		raise NotImplementedError

	@property
	def is_serving(self) -> bool:
		raise NotImplementedError

	def install(self, on_output: Callable[[str], None] | None = None) -> None:
		"""Render every machine's config, then apply each firewall and install nginx, then publish
		DNS. Rendering goes first so a bad template changes nothing, and DNS last so it only
		points at a machine that serves. Safe to repeat."""
		configs = [(machine, self.render(machine)) for machine in self.machines]
		for machine, config in configs:
			machine.set_firewall(
				inbound=[
					{"protocol": "any", "cidrs": [machine.tenant_network]},
					{"protocol": "icmp", "cidrs": ANYWHERE},
					{"protocol": "tcp", "ports": "80", "cidrs": ANYWHERE},
					{"protocol": "tcp", "ports": "443", "cidrs": ANYWHERE},
				],
				outbound=[{"protocol": "any", "cidrs": ANYWHERE}],
			)
			environment = {"NGINX_CONFIG": config, "CONFIG_NAME": self.name}
			machine.ssh_client.run(
				read_script("cargo", *INSTALL_SCRIPT, environment=environment), on_output=on_output
			)

		self.publish_dns(self.machines, self.domains)

	def publish_dns(self, machines: list[Machine], domains: tuple[str, ...]) -> None:
		"""Point each domain at the machines' public IPv4 addresses for the internet, and its
		`internal-` name at their mesh addresses for tenant VMs. Replaces what the names had."""
		public_addresses = [machine.public_ipv4 for machine in machines]
		mesh_addresses = [machine.address for machine in machines]
		client = AtlasClient.from_settings()
		for domain in domains:
			client.set_dns_records(domain, public_addresses, health_check_url=self.health_check_url)
			client.set_dns_records(f"internal-{domain}", mesh_addresses)

	def update_dns_records(self) -> None:
		"""Publish DNS without a full setup, for an operator whose records drifted or whose
		machine got a new address."""
		try:
			self.publish_dns(self.machines, self.domains)
		except AtlasError as exception:
			frappe.throw(_("Atlas would not update the DNS records: {0}").format(str(exception)))

	def render(self, machine: Machine) -> str:
		"""The nginx config for one machine. A variable the template names and the environment
		lacks raises, rather than writing a server with a hole in it."""
		environment = {
			**self.get_environment(machine),
			"CERTIFICATE": f"{CERTIFICATE_DIRECTORY}/{self.name}.crt",
			"CERTIFICATE_KEY": f"{CERTIFICATE_DIRECTORY}/{self.name}.key",
		}
		return Template(self.template_path.read_text()).substitute(environment)

	@property
	def template_path(self) -> Path:
		return Path(frappe.get_app_path("cargo"), *self.template)

	def wait_until_serving(self) -> bool:
		"""DNS was just published, so the probe is given a minute to resolve."""
		for _attempt in range(PROBE_ATTEMPTS):
			if self.is_serving:
				return True
			time.sleep(PROBE_INTERVAL_SECONDS)

		return False


def require_public(machine: Machine | None, label: str) -> Machine:
	"""The machine, once it runs and Atlas has given it a public IPv4. Throws otherwise."""
	if not machine or machine.status != "Running":
		frappe.throw(_("The {0} is not running.").format(label))

	if not machine.public_ipv4:
		frappe.throw(_("The {0} ({1}) has no public IPv4 address yet.").format(label, machine.name))

	return machine


def answers(url: str, method: str = "GET") -> requests.Response | None:
	"""One HTTPS request with a verified certificate, or None when it never got an answer."""
	try:
		return requests.request(method, url, timeout=PROBE_TIMEOUT_SECONDS)
	except requests.RequestException:
		return None
