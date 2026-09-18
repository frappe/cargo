# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from frappe.tests import UnitTestCase

from cargo.auth import (
	ALGORITHM,
	TOKEN_HEADER,
	authenticate_request,
	issuer_for_key_id,
	token_claims,
	verify_token,
)

REGION_ID = 4
AUDIENCE = f"atlas-cargo:{REGION_ID}"
KEY_ID = "central:key-1"
ATLAS_KEY_ID = f"atlas:{REGION_ID}:key-1"
JWKS_URL = "https://central.test/api/method/central.api.jwks.get_jwks"
SETTINGS = SimpleNamespace(central_url="https://central.test/", region_id=REGION_ID, jwks_url=JWKS_URL)


def build_token(
	private_key,
	audience: str = AUDIENCE,
	expires_in: int = 300,
	key_id=KEY_ID,
	issuer: str = "central",
) -> str:
	headers = {"kid": key_id} if key_id else None
	now = datetime.now(UTC)
	return jwt.encode(
		{
			"iss": issuer,
			"sub": "central",
			"aud": audience,
			"iat": now,
			"exp": now + timedelta(seconds=expires_in),
			"scope": "cargo:atlas",
		},
		private_key,
		algorithm=ALGORITHM,
		headers=headers,
	)


class UnitTestAccessToken(UnitTestCase):
	"""Cargo accepts only the audience reserved for its regional API."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		cls.private_key = Ed25519PrivateKey.generate()

	@contextmanager
	def central_keys(self, public_key=None, key_algorithm: str = ALGORITHM):
		key = SimpleNamespace(key=public_key or self.private_key.public_key(), algorithm_name=key_algorithm)
		with (
			patch("frappe.get_cached_doc", return_value=SETTINGS),
			patch("cargo.auth.jwks_client", return_value=Mock()),
			patch("jwt.PyJWKClient.match_kid", return_value=key),
		):
			yield

	def claims_of(self, token: str, public_key=None, key_algorithm: str = ALGORITHM):
		with self.central_keys(public_key, key_algorithm):
			return token_claims(token)

	def test_a_token_signed_for_this_region_carries_its_claims(self):
		claims = self.claims_of(build_token(self.private_key))

		self.assertEqual(claims["aud"], AUDIENCE)
		self.assertEqual(claims["scope"], "cargo:atlas")

	def test_a_region_wide_central_audience_is_refused(self):
		"""Every Central token names the region it is for; one that names none is not ours."""
		self.assertIsNone(self.claims_of(build_token(self.private_key, audience="central-admin")))

	def test_centrals_bucket_audience_for_this_region_is_refused(self):
		self.assertIsNone(
			self.claims_of(build_token(self.private_key, audience=f"central-{REGION_ID}-bucket"))
		)

	def test_a_bucket_token_minted_for_another_region_is_refused(self):
		self.assertIsNone(self.claims_of(build_token(self.private_key, audience="central-9-bucket")))

	def test_a_token_minted_for_another_region_is_refused(self):
		self.assertIsNone(self.claims_of(build_token(self.private_key, audience="atlas-cargo:9")))

	def test_a_token_for_a_proxy_is_not_one_for_cargo(self):
		self.assertIsNone(self.claims_of(build_token(self.private_key, audience=f"atlas-proxy:{REGION_ID}")))

	def test_an_expired_token_is_refused(self):
		self.assertIsNone(self.claims_of(build_token(self.private_key, expires_in=-1)))

	def test_a_token_that_names_no_key_is_refused(self):
		self.assertIsNone(self.claims_of(build_token(self.private_key, key_id=None)))

	def test_a_token_signed_by_an_unknown_key_is_refused(self):
		other = Ed25519PrivateKey.generate()

		self.assertIsNone(self.claims_of(build_token(other)))

	def test_a_token_with_no_expiry_is_refused(self):
		token = jwt.encode(
			{"iss": "central", "sub": "central", "aud": AUDIENCE, "iat": datetime.now(UTC)},
			self.private_key,
			algorithm=ALGORITHM,
			headers={"kid": KEY_ID},
		)

		self.assertIsNone(self.claims_of(token))

	def test_an_unsigned_token_is_refused(self):
		token = jwt.encode({"aud": AUDIENCE, "exp": 9999999999}, key=None, algorithm="none")

		self.assertIsNone(self.claims_of(token))

	def test_a_token_naming_an_issuer_its_key_does_not_belong_to_is_refused(self):
		"""The point of the check: Central's key is on the same set as the region's Atlas key,
		so a Central-signed token claiming to be Atlas must not pass."""
		token = build_token(self.private_key, issuer=f"atlas:{REGION_ID}")

		self.assertIsNone(self.claims_of(token))

	def test_a_token_signed_by_the_regions_atlas_key_is_accepted(self):
		claims = self.claims_of(
			build_token(self.private_key, key_id=ATLAS_KEY_ID, issuer=f"atlas:{REGION_ID}")
		)

		self.assertEqual(claims["iss"], f"atlas:{REGION_ID}")

	def test_a_token_signed_by_another_regions_atlas_key_is_refused(self):
		token = build_token(self.private_key, key_id="atlas:9:key-1", issuer="atlas:9")

		self.assertIsNone(self.claims_of(token))

	def test_a_token_naming_a_key_outside_every_issuer_namespace_is_refused(self):
		self.assertIsNone(self.claims_of(build_token(self.private_key, key_id="key-1")))

	def test_a_token_naming_another_algorithm_is_refused(self):
		"""Refused on the header alone, before any key is fetched."""
		signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
		now = datetime.now(UTC)
		token = jwt.encode(
			{
				"iss": "central",
				"sub": "central",
				"aud": AUDIENCE,
				"iat": now,
				"exp": now + timedelta(seconds=300),
			},
			signing_key,
			algorithm="RS256",
			headers={"kid": KEY_ID},
		)

		with patch("cargo.auth.jwks_client") as client:
			self.assertIsNone(self.claims_of(token))

		client.assert_not_called()

	def test_a_key_published_for_another_algorithm_is_refused(self):
		"""A key set that ever carried a non-Ed25519 key must not verify a token."""
		self.assertIsNone(self.claims_of(build_token(self.private_key), key_algorithm="RS256"))

	def test_a_token_with_no_issuer_is_refused(self):
		token = jwt.encode(
			{
				"sub": "central",
				"aud": AUDIENCE,
				"iat": datetime.now(UTC),
				"exp": datetime.now(UTC) + timedelta(seconds=300),
			},
			self.private_key,
			algorithm=ALGORITHM,
			headers={"kid": KEY_ID},
		)

		self.assertIsNone(self.claims_of(token))


class UnitTestIssuerForKeyId(UnitTestCase):
	"""A key id names its issuer, and only the prefix decides."""

	def test_a_central_key_id_names_central(self):
		self.assertEqual(issuer_for_key_id("central:abc", REGION_ID), "central")

	def test_a_key_id_for_this_regions_atlas_names_that_atlas(self):
		self.assertEqual(issuer_for_key_id(f"atlas:{REGION_ID}:abc", REGION_ID), f"atlas:{REGION_ID}")

	def test_another_regions_atlas_key_id_names_no_issuer(self):
		self.assertIsNone(issuer_for_key_id("atlas:9:abc", REGION_ID))

	def test_an_unnamespaced_key_id_names_no_issuer(self):
		self.assertIsNone(issuer_for_key_id("abc", REGION_ID))

	def test_a_bare_namespace_with_no_key_names_no_issuer(self):
		self.assertIsNone(issuer_for_key_id("central:", REGION_ID))

	def test_an_issuer_name_that_only_starts_the_same_names_no_issuer(self):
		self.assertIsNone(issuer_for_key_id("central-other:abc", REGION_ID))


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
	"""Where the keys come from is configured, not derived: Central may publish them
	somewhere other than its own host."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		cls.private_key = Ed25519PrivateKey.generate()

	def test_the_configured_key_set_is_the_one_fetched(self):
		key = SimpleNamespace(key=self.private_key.public_key(), algorithm_name=ALGORITHM)
		with (
			patch("frappe.get_cached_doc", return_value=SETTINGS),
			patch("cargo.auth.jwks_client", return_value=Mock()) as client,
			patch("jwt.PyJWKClient.match_kid", return_value=key),
		):
			token_claims(build_token(self.private_key))

		client.assert_called_once_with(JWKS_URL)


class UnitTestAuthenticateRequest(UnitTestCase):
	def test_a_token_is_still_required_before_anything_is_fetched(self):
		with patch("frappe.get_request_header", return_value="   "):
			with self.assertRaises(frappe.AuthenticationError):
				authenticate_request()
