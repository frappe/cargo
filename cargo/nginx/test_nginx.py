from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

import frappe
import requests
import urllib3
from cloudtoolkit.ssh.client import SSHClient
from frappe.tests import UnitTestCase
from jinja2 import UndefinedError

from cargo.atlas_client import AtlasError
from cargo.nginx import PROBE_ATTEMPTS, Nginx, answers, answers_at
from cargo.testing import nginx_over_ssh


def machine(number: int) -> SimpleNamespace:
	return SimpleNamespace(
		name=f"gateway-{number}",
		address=f"fdaa:1::{number}",
		tenant_network="fdaa:1::/64",
		public_ipv4=f"203.0.113.{number}",
		ssh_client=SSHClient(f"fdaa:1::{number}", private_key="private-key"),
		set_firewall=Mock(),
	)


class SiteNginx(Nginx):
	name = "object-storage"
	template = ("object_storage", "conf", "nginx", "object-storage.conf")
	domains = ("s3-svc.example.test", "s3-admin-svc.example.test")

	def __init__(self, context: dict[str, str] | None = None) -> None:
		self.context = context
		self.nodes = [machine(7), machine(8)]

	@property
	def machines(self) -> list[SimpleNamespace]:
		return self.nodes

	def get_context(self, machine: SimpleNamespace) -> dict[str, str]:
		return self.context or {
			"domain": "example.test",
			"s3_upstream": f"[{machine.address}]:3900",
			"admin_upstream": f"[{machine.address}]:3903",
		}


class UnitTestNginx(UnitTestCase):
	"""Rendering, installing and probing, the parts every service's nginx shares."""

	def test_the_config_is_rendered_from_the_context(self):
		proxy = SiteNginx()
		rendered = proxy.render(proxy.nodes[0])

		self.assertIn("server_name s3-svc.example.test;", rendered)
		self.assertIn("proxy_pass http://[fdaa:1::7]:3900;", rendered)
		self.assertIn("ssl_certificate /etc/nginx/tls/object-storage.crt;", rendered)
		self.assertIn("proxy_set_header Host $http_host;", rendered)
		self.assertNotIn("{{", rendered)

	def test_a_variable_the_template_needs_and_lacks_raises(self):
		proxy = SiteNginx({"domain": "example.test"})

		with self.assertRaises(UndefinedError):
			proxy.render(proxy.nodes[0])

	def test_install_publishes_every_machine_once_each_serves(self):
		with patch("cargo.nginx.AtlasClient") as atlas, nginx_over_ssh() as (run, _):
			atlas.from_settings.return_value.set_dns_records.side_effect = lambda *_, **__: self.assertEqual(
				run.call_count, 6
			)
			SiteNginx().install()

		published = [call.args for call in atlas.from_settings.return_value.set_dns_records.call_args_list]
		self.assertEqual(
			published,
			[
				("s3-svc.example.test", ["203.0.113.7", "203.0.113.8"]),
				("internal-s3-svc.example.test", ["fdaa:1::7", "fdaa:1::8"]),
				("s3-admin-svc.example.test", ["203.0.113.7", "203.0.113.8"]),
				("internal-s3-admin-svc.example.test", ["fdaa:1::7", "fdaa:1::8"]),
			],
		)

	def test_each_machine_gets_its_own_config(self):
		proxy = SiteNginx()
		with patch("cargo.nginx.AtlasClient"), nginx_over_ssh() as (_, configs):
			proxy.install()

		self.assertEqual(configs, {node.address: proxy.render(node) for node in proxy.nodes})

	def test_install_admits_the_web_and_tenant_zero_vms_first(self):
		proxy = SiteNginx()
		with patch("cargo.nginx.AtlasClient"), nginx_over_ssh() as (run, _):
			proxy.nodes[0].set_firewall.side_effect = lambda **_: run.assert_not_called()
			proxy.install()

		firewall = proxy.nodes[0].set_firewall.call_args.kwargs
		self.assertIn({"protocol": "any", "cidrs": ["fdaa:1::/64"]}, firewall["inbound"])
		self.assertIn(
			{"protocol": "tcp", "ports": "443", "cidrs": ["0.0.0.0/0", "::/0"]}, firewall["inbound"]
		)
		self.assertEqual(firewall["outbound"], [{"protocol": "any", "cidrs": ["0.0.0.0/0", "::/0"]}])

	def test_a_config_that_cannot_render_changes_nothing(self):
		proxy = SiteNginx({"domain": "example.test"})
		with patch("cargo.nginx.AtlasClient") as atlas, patch.object(SSHClient, "run", autospec=True) as run:
			with self.assertRaises(UndefinedError):
				proxy.install()

		proxy.nodes[0].set_firewall.assert_not_called()
		atlas.from_settings.return_value.set_dns_records.assert_not_called()
		run.assert_not_called()

	def test_updating_dns_publishes_without_installing(self):
		proxy = SiteNginx()
		with patch("cargo.nginx.AtlasClient") as atlas, patch.object(SSHClient, "run", autospec=True) as run:
			proxy.update_dns_records()

		self.assertEqual(atlas.from_settings.return_value.set_dns_records.call_count, 4)
		proxy.nodes[0].set_firewall.assert_not_called()
		run.assert_not_called()

	def test_atlas_refusing_a_dns_update_says_why(self):
		with patch("cargo.nginx.AtlasClient") as atlas:
			atlas.from_settings.return_value.set_dns_records.side_effect = AtlasError("409 zone_locked")
			with self.assertRaisesRegex(frappe.ValidationError, "zone_locked"):
				SiteNginx().update_dns_records()

	def test_nginx_is_installed_then_reloaded_only_once_the_config_passes(self):
		with patch("cargo.nginx.AtlasClient"), nginx_over_ssh() as (run, _):
			SiteNginx().install()

		scripts = [call.args[1] for call in run.call_args_list if call.args[0].host == "fdaa:1::7"]
		self.assertIn("apt-get install -y nginx", scripts[0])
		self.assertEqual(scripts[1], "install -d -m 700 /etc/nginx/tls")
		self.assertEqual(scripts[2], "nginx -t && systemctl reload nginx")

	def test_the_certificate_and_key_land_where_the_config_reads_them(self):
		proxy = SiteNginx()
		certificate = Mock(certificate="certificate-pem")
		certificate.get_password.return_value = "private-key-pem"
		copied = {}

		def put(client, source, destination, **_):
			copied[destination] = Path(source).read_text()

		with (
			patch.object(SSHClient, "run", autospec=True),
			patch.object(SSHClient, "rsync", autospec=True, side_effect=put),
		):
			proxy.add_certificate(proxy.nodes[0], certificate)

		rendered = proxy.render(proxy.nodes[0])
		self.assertEqual(copied[proxy.certificate_path], "certificate-pem")
		self.assertEqual(copied[proxy.certificate_key_path], "private-key-pem")
		self.assertIn(f"ssl_certificate {proxy.certificate_path};", rendered)
		self.assertIn(f"ssl_certificate_key {proxy.certificate_key_path};", rendered)
		certificate.get_password.assert_called_once_with("private_key")

	def test_a_certificate_that_cannot_be_issued_changes_no_machine(self):
		proxy = SiteNginx()
		with (
			patch("cargo.nginx.AtlasClient") as atlas,
			patch("cargo.nginx.get_service_certificate", side_effect=RuntimeError("acme refused")),
			patch.object(SSHClient, "run", autospec=True) as run,
			self.assertRaisesRegex(RuntimeError, "acme refused"),
		):
			proxy.install()

		proxy.nodes[0].set_firewall.assert_not_called()
		atlas.from_settings.return_value.set_dns_records.assert_not_called()
		run.assert_not_called()

	def test_adding_puts_the_certificate_on_every_machine_and_reloads(self):
		certificate = Mock(certificate="certificate-pem")
		certificate.get_password.return_value = "private-key-pem"
		with nginx_over_ssh() as (run, _):
			SiteNginx().add_certificate_to_machines(certificate)

		scripts = [(call.args[0].host, call.args[1]) for call in run.call_args_list]
		for host in ("fdaa:1::7", "fdaa:1::8"):
			self.assertIn((host, "install -d -m 700 /etc/nginx/tls"), scripts)
			self.assertIn((host, "nginx -t && systemctl reload nginx"), scripts)

	def test_waiting_retries_until_the_domain_answers(self):
		with (
			patch.object(SiteNginx, "is_serving", new_callable=PropertyMock, side_effect=[False, True]),
			patch("time.sleep") as slept,
		):
			self.assertTrue(SiteNginx().wait_until_serving())

		slept.assert_called_once()

	def test_waiting_gives_up_after_a_bounded_number_of_probes(self):
		with (
			patch.object(SiteNginx, "is_serving", new_callable=PropertyMock, return_value=False) as probe,
			patch("time.sleep"),
		):
			self.assertFalse(SiteNginx().wait_until_serving())

		self.assertEqual(probe.call_count, PROBE_ATTEMPTS)

	def test_a_request_that_never_got_an_answer_is_none(self):
		for error in (requests.ConnectionError("no route"), requests.exceptions.SSLError("self-signed")):
			with self.subTest(error=type(error).__name__), patch("requests.request", side_effect=error):
				self.assertIsNone(answers("https://example.test/health"))

	def test_asking_one_address_still_checks_the_certificate_against_the_name(self):
		with patch("urllib3.HTTPSConnectionPool") as pool:
			pool.return_value.request.return_value.status = 200
			status = answers_at("203.0.113.7", "https://s3-admin-svc.example.test/health")

		self.assertEqual(status, 200)
		self.assertEqual(pool.call_args.args, ("203.0.113.7", 443))
		self.assertEqual(pool.call_args.kwargs["assert_hostname"], "s3-admin-svc.example.test")
		self.assertEqual(pool.call_args.kwargs["server_hostname"], "s3-admin-svc.example.test")
		pool.return_value.request.assert_called_once_with(
			"GET", "/health", headers={"Host": "s3-admin-svc.example.test"}
		)

	def test_an_address_that_never_answered_is_none(self):
		with patch("urllib3.HTTPSConnectionPool") as pool:
			pool.return_value.request.side_effect = urllib3.exceptions.SSLError("hostname mismatch")

			self.assertIsNone(answers_at("203.0.113.7", "https://s3-admin-svc.example.test/health"))
