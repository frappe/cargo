from unittest.mock import Mock, patch

import requests
from frappe.tests import UnitTestCase

from cargo.proxy_client import MAP_ATTEMPTS, ProxyClient, ProxyError


class UnitTestProxyClient(UnitTestCase):
	def setUp(self) -> None:
		self.client = ProxyClient("https://proxy.example.com/", "proxy-token", "example.com")
		# The backoff is what the retries cost; the test only cares that they happen.
		patcher = patch("cargo.proxy_client.time.sleep")
		self.sleep = patcher.start()
		self.addCleanup(patcher.stop)

	def test_a_domain_maps_to_its_site_name_and_mesh_address(self) -> None:
		response = Mock(ok=True)
		with patch("cargo.proxy_client.requests.patch", return_value=response) as request:
			self.client.map_domain("s3-svc.example.com", "fdaa:1::10")

		request.assert_called_once_with(
			"https://proxy.example.com/v1/sites/s3-svc",
			headers={"Authorization": "Bearer proxy-token"},
			json={"address": "fdaa:1::10"},
			timeout=5,
		)

	def test_a_domain_outside_the_wildcard_is_refused(self) -> None:
		with (
			patch("cargo.proxy_client.requests.patch") as request,
			self.assertRaises(ProxyError),
		):
			self.client.map_domain("s3-svc.other.example", "fdaa:1::10")

		request.assert_not_called()

	def test_a_nested_domain_is_refused(self) -> None:
		with self.assertRaises(ProxyError):
			self.client.map_domain("bucket.s3-svc.example.com", "fdaa:1::10")

	def test_a_domain_without_the_token_suffix_is_refused(self) -> None:
		with (
			patch("cargo.proxy_client.requests.patch") as request,
			self.assertRaisesRegex(ProxyError, "permitted -svc site suffix"),
		):
			self.client.map_domain("s3.example.com", "fdaa:1::10")

		request.assert_not_called()

	def test_a_proxy_refusal_is_reported_and_not_repeated(self) -> None:
		"""A refusal is the Proxy's answer, so asking again only spends time."""
		response = Mock(ok=False, status_code=403, text="constraint denied")
		with (
			patch("cargo.proxy_client.requests.patch", return_value=response) as request,
			self.assertRaisesRegex(ProxyError, "constraint denied"),
		):
			self.client.map_domain("s3-svc.example.com", "fdaa:1::10")

		request.assert_called_once()

	def test_a_connection_failure_is_reported_after_every_attempt(self) -> None:
		with (
			patch(
				"cargo.proxy_client.requests.patch",
				side_effect=requests.ConnectionError("unreachable"),
			) as request,
			self.assertRaisesRegex(ProxyError, "unreachable"),
		):
			self.client.map_domain("s3-svc.example.com", "fdaa:1::10")

		self.assertEqual(request.call_count, MAP_ATTEMPTS)

	def test_a_mapping_that_succeeds_on_a_retry_is_not_reported(self) -> None:
		responses = [requests.ConnectionError("unreachable"), Mock(ok=False, status_code=503, text="")]
		with patch("cargo.proxy_client.requests.patch") as request:
			request.side_effect = [*responses, Mock(ok=True)]
			self.client.map_domain("s3-svc.example.com", "fdaa:1::10")

		self.assertEqual(request.call_count, MAP_ATTEMPTS)

	def test_the_backoff_grows_with_each_attempt(self) -> None:
		with (
			patch("cargo.proxy_client.requests.patch", side_effect=requests.ConnectionError("unreachable")),
			self.assertRaises(ProxyError),
		):
			self.client.map_domain("s3-svc.example.com", "fdaa:1::10")

		self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [1, 2])

	def test_a_busy_proxy_is_asked_again(self) -> None:
		response = Mock(ok=False, status_code=503, text="restarting")
		with (
			patch("cargo.proxy_client.requests.patch", return_value=response) as request,
			self.assertRaisesRegex(ProxyError, "restarting"),
		):
			self.client.map_domain("s3-svc.example.com", "fdaa:1::10")

		self.assertEqual(request.call_count, MAP_ATTEMPTS)
