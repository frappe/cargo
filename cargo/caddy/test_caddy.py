import shlex
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import requests
from frappe.tests import UnitTestCase

from cargo.caddy import PROBE_ATTEMPTS, Caddy, answers

MACHINE = SimpleNamespace(address="fdaa:1::7", get_password=lambda _fieldname: "private-key")


ENVIRONMENT = {
	"WILDCARD_DOMAIN": "example.test",
	"S3_UPSTREAM": "[fdaa:1::7]:3900",
	"ADMIN_UPSTREAM": "[fdaa:1::7]:3903",
	"CORS_ORIGIN": "https://central.test",
}


class SiteCaddy(Caddy):
	caddyfile = ("object_storage", "conf", "caddy", "Caddyfile")
	machine = MACHINE

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

	def test_a_variable_the_template_needs_and_lacks_raises(self):
		with self.assertRaises(KeyError):
			SiteCaddy({"WILDCARD_DOMAIN": "example.test"}).render()

	def test_install_sends_the_rendered_caddyfile_to_the_machine(self):
		with patch("cargo.caddy.run_over_ssh") as run:
			SiteCaddy().install()

		address, sent, key = run.call_args.args
		self.assertEqual((address, key), ("fdaa:1::7", "private-key"))
		self.assertIn(f"export CADDYFILE={shlex.quote(SiteCaddy().render())}", sent)
		self.assertIn("caddy validate", sent)

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
