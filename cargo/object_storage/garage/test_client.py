from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import UnitTestCase

from cargo.object_storage.garage.client import Client


def client_for(address: str) -> Client:
	return Client(SimpleNamespace(gateway_address=address, admin_port=3903))


class UnitTestClientUrl(UnitTestCase):
	"""Cargo reaches the admin API over the mesh, where Atlas hands out IPv6 addresses."""

	def test_a_mesh_address_keeps_its_colons_apart_from_the_port(self):
		self.assertEqual(client_for("fd00:1:2::5").url, "http://[fd00:1:2::5]:3903")

	def test_a_hostname_is_used_as_it_is(self):
		self.assertEqual(client_for("osc-0001-gateway").url, "http://osc-0001-gateway:3903")


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
