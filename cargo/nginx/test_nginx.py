import shlex
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

import frappe
import requests
from cloudtoolkit.ssh.client import SSHClient
from frappe.tests import UnitTestCase

from cargo.atlas_client import AtlasError
from cargo.nginx import PROBE_ATTEMPTS, Nginx, answers


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

	def __init__(self, environment: dict[str, str] | None = None) -> None:
		self.variables = environment
		self.nodes = [machine(7), machine(8)]

	@property
	def machines(self) -> list[SimpleNamespace]:
		return self.nodes

	def get_environment(self, machine: SimpleNamespace) -> dict[str, str]:
		return self.variables or {
			"WILDCARD_DOMAIN": "example.test",
			"S3_UPSTREAM": f"[{machine.address}]:3900",
			"ADMIN_UPSTREAM": f"[{machine.address}]:3903",
		}


class UnitTestNginx(UnitTestCase):
	"""Rendering, installing and probing, the parts every service's nginx shares."""

	def test_the_config_is_rendered_from_the_environment(self):
		proxy = SiteNginx()
		rendered = proxy.render(proxy.nodes[0])

		self.assertIn("server_name s3-svc.example.test;", rendered)
		self.assertIn("proxy_pass http://[fdaa:1::7]:3900;", rendered)
		self.assertIn("ssl_certificate /etc/nginx/tls/object-storage.crt;", rendered)
		self.assertIn("proxy_set_header Host $http_host;", rendered)
		self.assertNotIn("${", rendered.split("\n", 1)[1])

	def test_a_variable_the_template_needs_and_lacks_raises(self):
		proxy = SiteNginx({"WILDCARD_DOMAIN": "example.test"})

		with self.assertRaises(KeyError):
			proxy.render(proxy.nodes[0])

	def test_install_publishes_every_machine_once_each_serves(self):
		with patch("cargo.nginx.AtlasClient") as atlas, patch.object(SSHClient, "run", autospec=True) as run:
			atlas.from_settings.return_value.set_dns_records.side_effect = lambda *_, **__: self.assertEqual(
				run.call_count, 2
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
		with patch("cargo.nginx.AtlasClient"), patch.object(SSHClient, "run", autospec=True) as run:
			proxy.install()

		sent = {call.args[0].host: call.args[1] for call in run.call_args_list}
		self.assertEqual(set(sent), {"fdaa:1::7", "fdaa:1::8"})
		self.assertIn(f"export NGINX_CONFIG={shlex.quote(proxy.render(proxy.nodes[1]))}", sent["fdaa:1::8"])
		self.assertIn("export CONFIG_NAME=object-storage", sent["fdaa:1::8"])

	def test_install_admits_the_web_and_tenant_zero_vms_first(self):
		proxy = SiteNginx()
		with patch("cargo.nginx.AtlasClient"), patch.object(SSHClient, "run", autospec=True) as run:
			proxy.nodes[0].set_firewall.side_effect = lambda **_: run.assert_not_called()
			proxy.install()

		firewall = proxy.nodes[0].set_firewall.call_args.kwargs
		self.assertIn({"protocol": "any", "cidrs": ["fdaa:1::/64"]}, firewall["inbound"])
		self.assertIn(
			{"protocol": "tcp", "ports": "443", "cidrs": ["0.0.0.0/0", "::/0"]}, firewall["inbound"]
		)
		self.assertEqual(firewall["outbound"], [{"protocol": "any", "cidrs": ["0.0.0.0/0", "::/0"]}])

	def test_a_config_that_cannot_render_changes_nothing(self):
		proxy = SiteNginx({"WILDCARD_DOMAIN": "example.test"})
		with patch("cargo.nginx.AtlasClient") as atlas, patch.object(SSHClient, "run", autospec=True) as run:
			with self.assertRaises(KeyError):
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

	def test_caddy_is_stopped_only_after_the_new_config_passes(self):
		"""A config nginx refuses, such as one naming a missing certificate, leaves Caddy serving."""
		with patch("cargo.nginx.AtlasClient"), patch.object(SSHClient, "run", autospec=True) as run:
			SiteNginx().install()

		sent = run.call_args.args[1]
		self.assertLess(sent.index("nginx -t"), sent.index("systemctl disable --now caddy"))
		self.assertLess(sent.index("policy-rc.d"), sent.index("apt-get install -y nginx"))

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
