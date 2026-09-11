# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from frappe.tests import UnitTestCase

from cargo.auth import (
	TOKEN_HEADER,
	authenticate_request,
	jwks_url,
	token_claims,
	verify_token,
)

REGION_ID = 4
AUDIENCE = f"atlas-{REGION_ID}-admin"
KEY_ID = "key-1"
SETTINGS = SimpleNamespace(central_url="https://central.test/", region_id=REGION_ID)


def build_token(private_key, audience: str = AUDIENCE, expires_in: int = 300, key_id=KEY_ID) -> str:
	headers = {"kid": key_id} if key_id else None
	return jwt.encode(
		{"aud": audience, "exp": datetime.now(UTC) + timedelta(seconds=expires_in), "scope": "cargo:atlas"},
		private_key,
		algorithm="RS256",
		headers=headers,
	)


class UnitTestAccessToken(UnitTestCase):
	"""Central signs for itself and for Atlas; Cargo takes either, and nothing else."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

	@contextmanager
	def central_keys(self, public_key=None):
		key = SimpleNamespace(key=public_key or self.private_key.public_key())
		with (
			patch("frappe.get_cached_doc", return_value=SETTINGS),
			patch("cargo.auth.jwks_client", return_value=Mock()),
			patch("jwt.PyJWKClient.match_kid", return_value=key),
		):
			yield

	def claims_of(self, token: str, public_key=None):
		with self.central_keys(public_key):
			return token_claims(token)

	def test_a_token_signed_for_this_region_carries_its_claims(self):
		claims = self.claims_of(build_token(self.private_key))

		self.assertEqual(claims["aud"], AUDIENCE)
		self.assertEqual(claims["scope"], "cargo:atlas")

	def test_a_region_wide_central_audience_is_refused(self):
		"""Every Central token names the region it is for; one that names none is not ours."""
		self.assertIsNone(self.claims_of(build_token(self.private_key, audience="central-admin")))

	def test_centrals_bucket_audience_for_this_region_is_accepted(self):
		"""What Central mints per Cargo Instance for bucket work."""
		token = build_token(self.private_key, audience=f"central-{REGION_ID}-bucket")

		self.assertIsNotNone(self.claims_of(token))

	def test_a_bucket_token_minted_for_another_region_is_refused(self):
		self.assertIsNone(self.claims_of(build_token(self.private_key, audience="central-9-bucket")))

	def test_a_token_minted_for_another_region_is_refused(self):
		self.assertIsNone(self.claims_of(build_token(self.private_key, audience="atlas-9-admin")))

	def test_a_token_for_a_proxy_is_not_one_for_cargo(self):
		self.assertIsNone(self.claims_of(build_token(self.private_key, audience=f"atlas-{REGION_ID}-proxy")))

	def test_an_expired_token_is_refused(self):
		self.assertIsNone(self.claims_of(build_token(self.private_key, expires_in=-1)))

	def test_a_token_that_names_no_key_is_refused(self):
		self.assertIsNone(self.claims_of(build_token(self.private_key, key_id=None)))

	def test_a_token_signed_by_anything_but_central_is_refused(self):
		other = rsa.generate_private_key(public_exponent=65537, key_size=2048)

		self.assertIsNone(self.claims_of(build_token(other)))

	def test_a_token_with_no_expiry_is_refused(self):
		token = jwt.encode({"aud": AUDIENCE}, self.private_key, algorithm="RS256", headers={"kid": KEY_ID})

		self.assertIsNone(self.claims_of(token))

	def test_an_unsigned_token_is_refused(self):
		token = jwt.encode({"aud": AUDIENCE, "exp": 9999999999}, key=None, algorithm="none")

		self.assertIsNone(self.claims_of(token))


class UnitTestVerifyToken(UnitTestCase):
	"""What the decorator does to a request, given a token that does or does not verify."""

	def handler(self):
		@verify_token
		def create_bucket(name: str) -> str:
			return name

		return create_bucket

	@contextmanager
	def request(self, token: str | None, claims: dict | None):
		with (
			patch.object(frappe.local, "request", frappe._dict(path="/api/method/x"), create=True),
			patch("frappe.get_request_header", return_value=token) as header,
			patch("cargo.auth.token_claims", return_value=claims),
		):
			yield header

	def test_a_verified_token_runs_the_handler_and_leaves_its_claims_behind(self):
		with self.request("a-token", {"aud": AUDIENCE, "instance": "cargo-1"}) as header:
			self.assertEqual(self.handler()("data"), "data")

		header.assert_called_with(TOKEN_HEADER)
		self.assertEqual(frappe.local.request_claims.instance, "cargo-1")

	def test_a_request_with_no_token_never_reaches_the_handler(self):
		with self.request(None, {"aud": AUDIENCE}), self.assertRaises(frappe.AuthenticationError):
			self.handler()("data")

	def test_a_token_that_does_not_verify_never_reaches_the_handler(self):
		with self.request("a-token", None), self.assertRaises(frappe.AuthenticationError):
			self.handler()("data")

	def test_a_call_arriving_outside_a_request_carries_no_token(self):
		"""A guest endpoint reached from a job or the desk is refused, not raised at."""
		with patch("cargo.auth.token_claims") as claims:
			with self.assertRaises(frappe.AuthenticationError):
				self.handler()("data")

		claims.assert_not_called()

	def test_the_handler_keeps_its_own_signature(self):
		"""Frappe maps request arguments off it, so the wrapper cannot hide it."""
		self.assertEqual(self.handler().__name__, "create_bucket")


class UnitTestJwksUrl(UnitTestCase):
	def test_the_key_set_hangs_off_central_url(self):
		self.assertEqual(jwks_url(SETTINGS), "https://central.test/api/method/central.api.jwks.get_jwks")

	def test_a_cargo_that_knows_no_central_cannot_verify_anything(self):
		with self.assertRaises(frappe.ValidationError):
			jwks_url(SimpleNamespace(central_url=None, region_id=REGION_ID))


class UnitTestAuthenticateRequest(UnitTestCase):
	def test_a_token_is_still_required_before_anything_is_fetched(self):
		with patch("frappe.get_request_header", return_value="   "):
			with self.assertRaises(frappe.AuthenticationError):
				authenticate_request()
