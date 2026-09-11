from types import SimpleNamespace

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
