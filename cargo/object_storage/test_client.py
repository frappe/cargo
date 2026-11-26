from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
import requests
from frappe.tests import UnitTestCase
from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError

from cargo.object_storage.client import Client, Error, NoGateway, Unreachable


def client_for(*addresses: str) -> Client:
	cluster = SimpleNamespace(gateway_addresses=list(addresses), admin_port=3903, get_password=lambda _: "t")
	return Client(cluster)


def refused(url: str) -> requests.ConnectionError:
	return requests.ConnectionError(MaxRetryError(None, url, NewConnectionError(None, "refused")))


class UnitTestClientUrl(UnitTestCase):
	"""Cargo reaches the admin API over the mesh, where Atlas hands out IPv6 addresses."""

	def test_a_mesh_address_keeps_its_colons_apart_from_the_port(self):
		self.assertEqual(client_for("fd00:1:2::5").urls, ["http://[fd00:1:2::5]:3903"])

	def test_a_hostname_is_used_as_it_is(self):
		self.assertEqual(client_for("osc-0001-gateway").urls, ["http://osc-0001-gateway:3903"])


class UnitTestGatewayFailover(UnitTestCase):
	"""Any gateway answers the admin API, so a gateway that is down is skipped."""

	def test_a_gateway_that_refuses_the_connection_hands_over_to_the_next(self):
		answer = Mock(ok=True, status_code=200, content=b"{}", json=lambda: {"ok": True})
		with patch("requests.request", side_effect=[refused("first"), answer]) as sent:
			self.assertEqual(client_for("fd00::1", "fd00::2").status(), {"ok": True})

		self.assertIn("[fd00::2]", sent.call_args_list[1].args[1])

	def test_every_gateway_down_is_unreachable(self):
		with (
			patch("requests.request", side_effect=[refused("first"), refused("second")]),
			self.assertRaises(Unreachable),
		):
			client_for("fd00::1", "fd00::2").status()

	def test_no_gateway_is_a_validation_error_not_a_failed_call(self):
		with patch("requests.request") as sent, self.assertRaises(NoGateway) as raised:
			client_for().status()

		sent.assert_not_called()
		self.assertIsInstance(raised.exception, frappe.ValidationError)
		self.assertNotIsInstance(raised.exception, Error)

	def test_a_read_timeout_is_not_unreachable(self):
		"""The gateway took the request, so the cluster was reached."""
		with (
			patch("requests.request", side_effect=requests.ReadTimeout("slow")),
			self.assertRaises(Error) as raised,
		):
			client_for("fd00::1").status()

		self.assertNotIsInstance(raised.exception, Unreachable)

	def test_a_timeout_can_be_set_for_one_client(self):
		cluster = SimpleNamespace(gateway_addresses=["fd00::1"], admin_port=3903, get_password=lambda _: "t")
		answer = Mock(ok=True, status_code=200, content=b"{}", json=lambda: {})
		with patch("requests.request", return_value=answer) as sent:
			Client(cluster, timeout=5).status()
			Client(cluster).status()

		self.assertEqual([call.kwargs["timeout"] for call in sent.call_args_list], [5, 30])

	def test_a_call_cut_off_after_it_was_sent_is_not_repeated(self):
		"""CreateKey sent twice would make two keys."""
		aborted = requests.ConnectionError(ProtocolError("Connection aborted."))
		with patch("requests.request", side_effect=[aborted]) as sent, self.assertRaises(Error) as raised:
			client_for("fd00::1", "fd00::2").create_key("k")

		self.assertEqual(sent.call_count, 1)
		self.assertNotIsInstance(raised.exception, Unreachable)


class UnitTestBucketQuota(UnitTestCase):
	"""The v2 admin API names its endpoints and takes the bucket by query parameter. A v1
	path answers 400, which reads as a server fault by the time it reaches Central."""

	def quota_call(self, *args, **kwargs):
		with patch.object(Client, "call", return_value=None) as call:
			client_for("osc-0001-gateway").set_bucket_quota(*args, **kwargs)
		return call

	def test_a_quota_is_sent_to_the_v2_update_endpoint(self):
		call = self.quota_call("b1", 1073741824)

		self.assertEqual(call.call_args.args, ("UpdateBucket", "POST"))
		self.assertEqual(call.call_args.kwargs["params"], {"id": "b1"})

	def test_both_caps_go_in_one_quotas_object(self):
		"""Garage names it `quotas`, and reads the pair together: one sent alone clears
		the other."""
		call = self.quota_call("b1", 1073741824, 100)

		self.assertEqual(
			call.call_args.kwargs["json"],
			{"quotas": {"maxSize": 1073741824, "maxObjects": 100}},
		)

	def test_no_cap_is_sent_as_null_rather_than_left_out(self):
		call = self.quota_call("b1", None)

		self.assertEqual(call.call_args.kwargs["json"], {"quotas": {"maxSize": None, "maxObjects": None}})
