from __future__ import annotations

import time
import typing
from collections.abc import Callable
from pathlib import Path
from string import Template

import frappe
import requests
from frappe import _

from cargo.atlas_client import ANYWHERE, AtlasClient, AtlasError
from cargo.ssh import SSH_TIMEOUT, run_over_ssh, script

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.machine.machine import Machine

INSTALL_SCRIPT = ("caddy", "install.sh")
PROBE_TIMEOUT_SECONDS = 10
PROBE_ATTEMPTS = 6
PROBE_INTERVAL_SECONDS = 10


class Caddy:
	"""Caddy on a service's public machine. A subclass names the machine, its domains, the
	Caddyfile template, what fills it in, and how to tell the service answers."""

	caddyfile: tuple[str, ...]

	@property
	def machine(self) -> Machine:
		raise NotImplementedError

	@property
	def domains(self) -> tuple[str, ...]:
		raise NotImplementedError

	@property
	def environment(self) -> dict[str, str]:
		raise NotImplementedError

	@property
	def is_serving(self) -> bool:
		raise NotImplementedError

	def install(self, on_output: Callable[[str], None] | None = None) -> None:
		"""Render the Caddyfile, apply the firewall, publish DNS, then install Caddy. Rendering
		goes first so a bad template changes nothing outside Cargo. Safe to repeat: certificates
		are kept."""
		machine = self.machine
		caddyfile = self.render()
		machine.set_firewall(
			inbound=[
				{"protocol": "any", "cidrs": [machine.tenant_network]},
				{"protocol": "icmp", "cidrs": ANYWHERE},
				{"protocol": "tcp", "ports": "80", "cidrs": ANYWHERE},
				{"protocol": "tcp", "ports": "443", "cidrs": ANYWHERE},
			],
			outbound=[{"protocol": "any", "cidrs": ANYWHERE}],
		)
		self.publish_dns(machine)
		run_over_ssh(
			machine.address,
			script(*INSTALL_SCRIPT, environment={"CADDYFILE": caddyfile}),
			machine.get_password("ssh_private_key"),
			timeout=SSH_TIMEOUT,
			on_output=on_output,
		)

	def publish_dns(self, machine: Machine) -> None:
		"""Point each domain at the public IPv4 for Let's Encrypt and the internet, and its
		`internal-` name at the mesh address for tenant VMs."""
		client = AtlasClient.from_settings()
		for domain in self.domains:
			client.set_dns_record(domain, machine.public_ipv4)
			client.set_dns_record(f"internal-{domain}", machine.address)

	def update_dns_records(self) -> None:
		"""Publish DNS without a full setup, for an operator whose records drifted or whose
		machine got a new address."""
		try:
			self.publish_dns(self.machine)
		except AtlasError as exception:
			frappe.throw(_("Atlas would not update the DNS records: {0}").format(str(exception)))

	def render(self) -> str:
		"""The Caddyfile for this service. A variable the template names and the environment
		lacks raises, rather than writing a site with a hole in it."""
		return Template(self.caddyfile_path.read_text()).substitute(self.environment)

	@property
	def caddyfile_path(self) -> Path:
		"""Joined here, not by `get_app_path`, which lowercases every part: `Caddyfile` is not
		`caddyfile` on Linux."""
		return Path(frappe.get_app_path("cargo")).joinpath(*self.caddyfile)

	def wait_until_serving(self) -> bool:
		"""Caddy obtains its certificates after it starts, so the probe is given a minute."""
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
