from unittest.mock import Mock, patch

import requests
from frappe.tests import UnitTestCase

from cargo.proxy_client import ProxyClient, ProxyError


class UnitTestProxyClient(UnitTestCase):
	def setUp(self) -> None:
		self.client = ProxyClient("https://proxy.example.com/", "proxy-token", "example.com")

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

	def test_a_proxy_refusal_is_reported(self) -> None:
		response = Mock(ok=False, status_code=403, text="constraint denied")
		with (
			patch("cargo.proxy_client.requests.patch", return_value=response),
			self.assertRaisesRegex(ProxyError, "constraint denied"),
		):
			self.client.map_domain("s3-svc.example.com", "fdaa:1::10")

	def test_a_connection_failure_is_reported(self) -> None:
		with (
			patch(
				"cargo.proxy_client.requests.patch",
				side_effect=requests.ConnectionError("unreachable"),
			),
			self.assertRaisesRegex(ProxyError, "unreachable"),
		):
			self.client.map_domain("s3-svc.example.com", "fdaa:1::10")
