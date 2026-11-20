import importlib.util
import urllib.error
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import cargo

PROVIDER_PATH = Path(cargo.__file__).parent / "image_builder" / "conf" / "pilot" / "domain_provider.py"


def load_provider():
	specification = importlib.util.spec_from_file_location("domain_provider", PROVIDER_PATH)
	module = importlib.util.module_from_spec(specification)
	specification.loader.exec_module(module)
	return module


def http_error(code: int) -> urllib.error.HTTPError:
	return urllib.error.HTTPError("https://central.test", code, "error", {}, None)


class TestDomainProviderRetry(TestCase):
	def setUp(self):
		self.provider = load_provider()
		self.sleep = patch.object(self.provider.time, "sleep").start()
		self.addCleanup(patch.stopall)

	def test_retries_a_network_error_until_it_succeeds(self):
		failure = urllib.error.URLError("Temporary failure in name resolution")
		with patch.object(self.provider, "call", side_effect=[failure, failure, "ok"]) as call:
			self.assertEqual(self.provider.call_with_retry("register_domain", "POST", "a.test"), "ok")

		self.assertEqual(call.call_count, 3)
		self.sleep.assert_called_with(self.provider.RETRY_DELAY_SECONDS)

	def test_retries_a_server_error(self):
		with patch.object(self.provider, "call", side_effect=[http_error(502), "ok"]) as call:
			self.provider.call_with_retry("deregister_domain", "POST", "a.test")

		self.assertEqual(call.call_count, 2)

	def test_does_not_retry_a_refusal(self):
		with patch.object(self.provider, "call", side_effect=http_error(409)) as call:
			with self.assertRaises(urllib.error.HTTPError):
				self.provider.call_with_retry("register_domain", "POST", "a.test")

		self.assertEqual(call.call_count, 1)
		self.sleep.assert_not_called()

	def test_gives_up_after_the_last_attempt(self):
		with patch.object(self.provider, "call", side_effect=TimeoutError()) as call:
			with self.assertRaises(TimeoutError):
				self.provider.call_with_retry("register_domain", "POST", "a.test")

		self.assertEqual(call.call_count, self.provider.ATTEMPTS)
		self.assertEqual(self.sleep.call_count, self.provider.ATTEMPTS - 1)
