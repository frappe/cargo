import time
from unittest.mock import Mock, patch

import frappe
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from frappe.tests import IntegrationTestCase
from jwt import PyJWK

from cargo.issuer import (
	ATLAS_KEYS_CACHE_KEY,
	MACHINE_AUDIENCE,
	generate_machine_token,
	get_merged_jwks,
	initialize_signing_key,
	issue_datum_token,
	verify_machine_token,
)
from cargo.testing import SETTINGS, use_test_settings

REGION_ID = SETTINGS["region_id"]
ATLAS_KEY = {"kty": "OKP", "crv": "Ed25519", "x": "atlas-public-key", "kid": f"atlas:{REGION_ID}:key-1"}


class IntegrationTestIssuer(IntegrationTestCase):
	"""Cargo signs the Datum tokens of its region's services with its own Ed25519 key."""

	def setUp(self):
		use_test_settings()
		initialize_signing_key()
		frappe.cache.delete_value(ATLAS_KEYS_CACHE_KEY)

	def merged_jwks(self, atlas_keys: list | None = None) -> dict:
		answer = Mock(**{"json.return_value": {"keys": atlas_keys or [ATLAS_KEY]}})
		with patch("cargo.issuer.requests.get", return_value=answer):
			return get_merged_jwks()

	def cargo_key(self) -> PyJWK:
		return PyJWK.from_dict(self.merged_jwks()["keys"][-1])

	def test_the_key_is_created_once_in_this_regions_namespace(self):
		key_id = frappe.db.get_single_value("Cargo Settings", "signing_key_id")
		initialize_signing_key()

		self.assertTrue(key_id.startswith(f"cargo:{REGION_ID}:"))
		self.assertEqual(frappe.db.get_single_value("Cargo Settings", "signing_key_id"), key_id)

	def test_a_datum_token_verifies_against_the_merged_key_set(self):
		token = issue_datum_token("vm-storage")

		claims = jwt.decode(
			token, self.cargo_key().key, algorithms=["EdDSA"], audience=f"atlas-datum:{REGION_ID}"
		)
		self.assertEqual(jwt.get_unverified_header(token)["kid"], self.cargo_key().key_id)
		self.assertEqual(claims["iss"], f"cargo:{REGION_ID}")
		self.assertEqual(claims["resource_id"], "vm-storage")
		self.assertEqual(claims["access"], ["write"])
		self.assertAlmostEqual(claims["exp"] - time.time(), 7 * 24 * 60 * 60, delta=60)

	def test_the_merged_key_set_keeps_the_atlas_keys(self):
		keys = self.merged_jwks()["keys"]

		self.assertEqual(keys[0], ATLAS_KEY)
		self.assertTrue(keys[1]["kid"].startswith(f"cargo:{REGION_ID}:"))
		self.assertNotIn("d", keys[1])

	def test_no_key_set_is_served_while_atlas_does_not_answer(self):
		with patch("cargo.issuer.requests.get", side_effect=ConnectionError):
			with self.assertRaises(ConnectionError):
				get_merged_jwks()

	def verify(self, token: str, status: str = "Running", vm_id: str = "vm-storage") -> str:
		machine = frappe._dict(status=status, vm_id=vm_id)
		with patch("cargo.issuer.frappe.db.get_value", return_value=machine):
			return verify_machine_token(token)

	def test_a_running_machine_gets_its_virtual_machine(self):
		self.assertEqual(
			self.verify(generate_machine_token("OSC-0001-storage-0001", "vm-storage")), "vm-storage"
		)

	def test_a_machine_that_is_not_running_is_refused(self):
		with self.assertRaises(frappe.AuthenticationError):
			self.verify(generate_machine_token("OSC-0001-storage-0001", "vm-storage"), status="Terminated")

	def test_a_token_for_another_virtual_machine_is_refused(self):
		with self.assertRaises(frappe.AuthenticationError):
			self.verify(generate_machine_token("OSC-0001-storage-0001", "vm-other"))

	def test_a_datum_token_is_not_a_machine_token(self):
		with self.assertRaises(frappe.AuthenticationError):
			self.verify(issue_datum_token("vm-storage"))

	def test_a_machine_token_signed_by_another_key_is_refused(self):
		token = jwt.encode(
			{
				"iss": f"cargo:{REGION_ID}",
				"sub": "m",
				"aud": MACHINE_AUDIENCE,
				"iat": 0,
				"virtual_machine": "vm-storage",
			},
			Ed25519PrivateKey.generate(),
			algorithm="EdDSA",
		)
		with self.assertRaises(frappe.AuthenticationError):
			self.verify(token)
