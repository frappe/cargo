from __future__ import annotations

import tempfile
import time
import typing
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import certifi
import frappe
import requests
import urllib3
from cloudtoolkit.ssh.client import read_script
from frappe import _
from jinja2 import StrictUndefined, Template

from cargo.atlas_client import ANYWHERE, AtlasClient, AtlasError
from cargo.tls.doctype.service_certificate.service_certificate import (
	ServiceCertificate,
	get_service_certificate,
)

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.machine.machine import Machine

INSTALL_SCRIPT = ("nginx", "install.sh")
CONFIG_DIRECTORY = "/etc/nginx/conf.d"
CERTIFICATE_DIRECTORY = "/etc/nginx/tls"
RELOAD = "nginx -t && systemctl reload nginx"
PROBE_TIMEOUT_SECONDS = 10
PROBE_ATTEMPTS = 6
PROBE_INTERVAL_SECONDS = 10


class Nginx:
	"""nginx on a service's public machines, all of them in DNS. A subclass names the machines,
	its domains, the config template, what fills it in, and how to tell the service answers. Its
	Service Certificate, named like its config, is copied to `CERTIFICATE_DIRECTORY` on each machine."""

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

	def get_context(self, machine: Machine) -> dict[str, str]:
		raise NotImplementedError

	@property
	def is_serving(self) -> bool:
		raise NotImplementedError

	def install(self, on_output: Callable[[str], None] | None = None) -> None:
		"""Render every machine's config and get the certificate, then on each machine set the
		firewall, install nginx and put the certificate and config, then publish DNS. Rendering and
		issuing go first so a failure there changes no machine, and DNS last so it only points at
		machines that serve. Safe to repeat."""
		configs = [(machine, self.render(machine)) for machine in self.machines]
		certificate = get_service_certificate(self.name, self.domains)
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
			machine.ssh_client.run(read_script("cargo", *INSTALL_SCRIPT), on_output=on_output)
			self.put_certificate(machine, certificate, on_output=on_output)
			self.put_config(machine, config, on_output=on_output)

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

	def put_config(
		self, machine: Machine, config: str, on_output: Callable[[str], None] | None = None
	) -> None:
		"""Copy the config to the machine, and reload nginx only if it passes `nginx -t`."""
		put_file(machine, config, f"{CONFIG_DIRECTORY}/{self.name}.conf", on_output=on_output)
		machine.ssh_client.run(RELOAD, on_output=on_output)

	def put_certificate(
		self,
		machine: Machine,
		certificate: ServiceCertificate,
		on_output: Callable[[str], None] | None = None,
	) -> None:
		"""Copy the certificate and its key into a folder only root can read."""
		machine.ssh_client.run(f"install -d -m 700 {CERTIFICATE_DIRECTORY}", on_output=on_output)
		put_file(machine, certificate.certificate, self.certificate_path, on_output=on_output)
		put_file(
			machine, certificate.get_password("private_key"), self.certificate_key_path, on_output=on_output
		)

	def renew_certificate(self) -> None:
		"""Put the certificate on every machine and reload nginx, re-issued first if it is due."""
		certificate = get_service_certificate(self.name, self.domains)
		for machine in self.machines:
			self.put_certificate(machine, certificate)
			machine.ssh_client.run(RELOAD)

	@property
	def certificate_path(self) -> str:
		return f"{CERTIFICATE_DIRECTORY}/{self.name}.crt"

	@property
	def certificate_key_path(self) -> str:
		return f"{CERTIFICATE_DIRECTORY}/{self.name}.key"

	def render(self, machine: Machine) -> str:
		"""The nginx config for one machine. A value the template names and the context lacks
		raises, rather than writing a server with a hole in it."""
		context = {
			**self.get_context(machine),
			"certificate": self.certificate_path,
			"certificate_key": self.certificate_key_path,
		}
		return Template(self.template_path.read_text(), undefined=StrictUndefined).render(context)

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


def put_file(
	machine: Machine, content: str, destination: str, on_output: Callable[[str], None] | None = None
) -> None:
	with tempfile.NamedTemporaryFile("w") as file:
		file.write(content)
		file.flush()
		machine.ssh_client.rsync(Path(file.name), destination, on_output=on_output)


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


def answers_at(address: str, url: str) -> int | None:
	"""The status `url` answers with at one `address`, its certificate still checked against the
	URL's host. None when it never got an answer. DNS would pick any of the addresses."""
	host = urlsplit(url).hostname
	pool = urllib3.HTTPSConnectionPool(
		address,
		443,
		server_hostname=host,
		assert_hostname=host,
		ca_certs=certifi.where(),
		timeout=PROBE_TIMEOUT_SECONDS,
		retries=False,
	)
	try:
		return pool.request("GET", urlsplit(url).path or "/", headers={"Host": host}).status
	except urllib3.exceptions.HTTPError:
		return None
	finally:
		pool.close()
