# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from cargo.atlas_client import AtlasClient, AtlasError, AtlasNotFound, error_message, host_port

TENANT_ID = 7


def response(status: int, payload=None, text: str = "") -> Mock:
	answer = Mock(status_code=status, ok=200 <= status < 300, text=text)
	answer.json.return_value = payload if payload is not None else {}
	if payload is None:
		answer.json.side_effect = ValueError("no body")

	return answer


class UnitTestAtlasClient(UnitTestCase):
	def setUp(self):
		self.client = AtlasClient("https://atlas.test/", "atlas-token", TENANT_ID)

	def call(self, answer: Mock):
		return patch("cargo.atlas_client.requests.request", return_value=answer)

	def test_every_request_carries_the_credentials_and_the_tenant(self):
		with self.call(response(200, {"id": "vm-1"})) as request:
			self.client.get_vm("vm-1")

		headers = request.call_args.kwargs["headers"]
		self.assertEqual(headers["Authorization"], "Bearer atlas-token")
		self.assertEqual(headers["X-Tenant-ID"], str(TENANT_ID))

	def test_create_asks_for_one_machine_of_the_given_shape(self):
		with self.call(response(201, {"id": "vm-9"})) as request:
			created = self.client.create_vm(
				image_id="ubuntu-24.04",
				vcpus=2,
				memory_mib=4096,
				disk_mib=20480,
				public_key="ssh-ed25519 AAAA",
				hostname="OSC-0001-storage-0001",
				metadata={"role": "storage"},
			)

		self.assertEqual(created["id"], "vm-9")
		body = request.call_args.kwargs["json"]
		self.assertEqual(body["vcpus"], 2)
		self.assertEqual(body["ssh_keys"], ["ssh-ed25519 AAAA"])
		self.assertEqual(body["metadata"], {"role": "storage"})
		# No public address is asked for: machines are reached over the mesh.
		self.assertNotIn("ip_address_id", body)
		self.assertEqual(body["egress"], "uplink")

	def test_a_created_machine_without_an_id_is_a_failure(self):
		with self.call(response(201, {})), self.assertRaises(AtlasError):
			self.client.create_vm(
				image_id="ubuntu-24.04",
				vcpus=1,
				memory_mib=1024,
				disk_mib=1024,
				public_key="key",
				hostname="host",
			)

	def test_a_missing_machine_is_its_own_error(self):
		with self.call(response(404, {"error": {"code": "not_found", "message": "gone"}})):
			with self.assertRaises(AtlasNotFound):
				self.client.get_vm("vm-1")

	def test_a_refusal_carries_atlas_own_message(self):
		payload = {"error": {"code": "conflict", "message": "attached", "fields": []}}
		with self.call(response(409, payload)):
			with self.assertRaises(AtlasError) as raised:
				self.client.terminate_vm("vm-1")

		self.assertIn("attached", str(raised.exception))
		self.assertNotIsInstance(raised.exception, AtlasNotFound)


class UnitTestAddressFormatting(UnitTestCase):
	def test_an_ipv6_address_keeps_its_colons(self):
		self.assertEqual(host_port("fdaa:1:0:7::3", 3903), "[fdaa:1:0:7::3]:3903")

	def test_a_name_or_ipv4_address_is_left_alone(self):
		self.assertEqual(host_port("cargo-vm1", 3903), "cargo-vm1:3903")
		self.assertEqual(host_port("10.0.0.1", "3903"), "10.0.0.1:3903")


class UnitTestErrorMessage(UnitTestCase):
	def test_atlas_fields_are_named_alongside_the_message(self):
		payload = {"error": {"message": "bad", "fields": [{"name": "vcpus", "message": "too many"}]}}

		self.assertEqual(error_message(payload, ""), "bad (vcpus: too many)")

	def test_a_body_that_is_not_json_falls_back_to_the_text(self):
		self.assertEqual(error_message(None, "  502 Bad Gateway  "), "502 Bad Gateway")

	def test_an_empty_body_still_says_something(self):
		self.assertEqual(error_message(None, ""), "unknown error")
