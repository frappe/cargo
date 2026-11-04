import shlex
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import requests
from frappe.tests import UnitTestCase

from cargo.caddy import PROBE_ATTEMPTS, Caddy, answers

MACHINE = SimpleNamespace(
	address="fdaa:1::7", public_ipv4="203.0.113.7", get_password=lambda _fieldname: "private-key"
)


ENVIRONMENT = {
	"WILDCARD_DOMAIN": "example.test",
	"S3_UPSTREAM": "[fdaa:1::7]:3900",
	"ADMIN_UPSTREAM": "[fdaa:1::7]:3903",
	"CORS_ORIGIN": "https://central.test",
}


class SiteCaddy(Caddy):
	caddyfile = ("object_storage", "conf", "caddy", "Caddyfile")
	machine = MACHINE
	domains = ("s3-svc.example.test", "s3-admin-svc.example.test")

	def __init__(self, environment: dict[str, str] | None = None) -> None:
		self.variables = environment or ENVIRONMENT

	@property
	def environment(self) -> dict[str, str]:
		return self.variables


class UnitTestCaddy(UnitTestCase):
	"""Rendering, installing and probing, the parts every service's Caddy shares."""

	def test_the_caddyfile_is_rendered_from_the_environment(self):
		rendered = SiteCaddy().render()

		self.assertIn("s3-svc.example.test {", rendered)
		self.assertIn("reverse_proxy [fdaa:1::7]:3900", rendered)
		self.assertNotIn("${", rendered)

	def test_the_template_path_keeps_its_case(self):
		"""`get_app_path` lowercases every part, which Linux reads as another file."""
		self.assertEqual(SiteCaddy().caddyfile_path.name, "Caddyfile")

	def test_a_variable_the_template_needs_and_lacks_raises(self):
		with self.assertRaises(KeyError):
			SiteCaddy({"WILDCARD_DOMAIN": "example.test"}).render()

	def test_install_publishes_public_and_internal_records_first(self):
		with patch("cargo.caddy.AtlasClient") as atlas, patch("cargo.caddy.run_over_ssh") as run:
			atlas.from_settings.return_value.set_dns_record.side_effect = lambda *_: run.assert_not_called()
			SiteCaddy().install()

		published = [call.args for call in atlas.from_settings.return_value.set_dns_record.call_args_list]
		self.assertEqual(
			published,
			[
				("s3-svc.example.test", "203.0.113.7"),
				("internal-s3-svc.example.test", "fdaa:1::7"),
				("s3-admin-svc.example.test", "203.0.113.7"),
				("internal-s3-admin-svc.example.test", "fdaa:1::7"),
			],
		)
		run.assert_called_once()

	def test_install_sends_the_rendered_caddyfile_to_the_machine(self):
		with patch("cargo.caddy.AtlasClient"), patch("cargo.caddy.run_over_ssh") as run:
			SiteCaddy().install()

		address, sent, key = run.call_args.args
		self.assertEqual((address, key), ("fdaa:1::7", "private-key"))
		self.assertIn(f"export CADDYFILE={shlex.quote(SiteCaddy().render())}", sent)
		self.assertIn("caddy validate", sent)

	def test_nginx_from_an_earlier_setup_is_stopped_before_caddy_starts(self):
		"""Earlier setups left nginx enabled on port 80, which Caddy needs for its certificates."""
		with patch("cargo.caddy.AtlasClient"), patch("cargo.caddy.run_over_ssh") as run:
			SiteCaddy().install()

		sent = run.call_args.args[1]
		self.assertIn("systemctl disable --now nginx", sent)
		self.assertLess(sent.index("disable --now nginx"), sent.index("apt-get install -y caddy"))

	def test_waiting_retries_until_the_certificate_lands(self):
		with (
			patch.object(SiteCaddy, "is_serving", new_callable=PropertyMock, side_effect=[False, True]),
			patch("time.sleep") as slept,
		):
			self.assertTrue(SiteCaddy().wait_until_serving())

		slept.assert_called_once()

	def test_waiting_gives_up_after_a_bounded_number_of_probes(self):
		with (
			patch.object(SiteCaddy, "is_serving", new_callable=PropertyMock, return_value=False) as probe,
			patch("time.sleep"),
		):
			self.assertFalse(SiteCaddy().wait_until_serving())

		self.assertEqual(probe.call_count, PROBE_ATTEMPTS)

	def test_a_request_that_never_got_an_answer_is_none(self):
		for error in (requests.ConnectionError("no route"), requests.exceptions.SSLError("self-signed")):
			with self.subTest(error=type(error).__name__), patch("requests.request", side_effect=error):
				self.assertIsNone(answers("https://example.test/health"))
