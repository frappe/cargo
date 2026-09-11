from __future__ import annotations

import functools
import typing
from collections.abc import Callable
from typing import Any

import frappe
from frappe import _

if typing.TYPE_CHECKING:
	from jwt import PyJWKClient

	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings

TOKEN_HEADER = "X-Cargo-Access-Token"
JWKS_ALGORITHMS = (
	"RS256",
	"RS384",
	"RS512",
	"ES256",
	"ES384",
	"ES512",
	"PS256",
	"PS384",
	"PS512",
	"EdDSA",
)

jwks_clients: dict[str, PyJWKClient] = {}


def verify_token(func: Callable) -> Callable:
	"""Authenticate the caller before the handler runs, leaving its claims on
	`frappe.local.request_claims`. functools.wraps is required — Frappe maps request
	arguments off the wrapped signature."""

	@functools.wraps(func)
	def wrapper(*args, **kwargs):
		frappe.local.request_claims = authenticate_request()
		return func(*args, **kwargs)

	return wrapper


def authenticate_request() -> frappe._dict:
	"""The claims of the token on this request, or an authentication error."""
	token = presented_token()
	if not token:
		frappe.throw(_("An access token is required."), frappe.AuthenticationError)

	claims = token_claims(token)
	if claims is None:
		frappe.throw(_("This access token is not one Cargo accepts."), frappe.AuthenticationError)

	return frappe._dict(claims)


def presented_token() -> str:
	"""The token this request carries. Nothing arriving outside a request carries one, so
	a background job or a desk call reaching a guest endpoint is refused rather than raised at."""
	if not getattr(frappe.local, "request", None):
		return ""

	return (frappe.get_request_header(TOKEN_HEADER) or "").strip()


def token_claims(token: str) -> dict[str, Any] | None:
	"""What one token carries, or None when it is not usable.

	Cargo verifies against the configured merged key set and holds no verification secret of its own."""
	import jwt
	from jwt import PyJWKClient

	settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")
	try:
		kid = jwt.get_unverified_header(token).get("kid")
		if not isinstance(kid, str):
			return None

		# An unknown key id must not make an attacker refetch the key set.
		signing_key = PyJWKClient.match_kid(jwks_client(settings.jwks_url).get_signing_keys(), kid)
		if signing_key is None:
			return None

		return jwt.decode(
			token,
			signing_key.key,
			algorithms=JWKS_ALGORITHMS,
			audience=accepted_audiences(settings),
			options={"require": ["exp", "aud"], "verify_aud": True},
		)
	except jwt.PyJWTError:
		return None


def accepted_audiences(settings: CargoSettings) -> list[str]:
	"""The audience reserved for this region's Cargo API."""
	return [f"atlas-cargo:{settings.region_id}"]


def jwks_client(url: str) -> PyJWKClient:
	"""One key set client per issuer: it caches the keys, so nothing refetches per request."""
	from jwt import PyJWKClient

	client = jwks_clients.get(url)
	if client is None:
		# A real user agent: the urllib default is turned away as a bot by some issuers.
		client = PyJWKClient(url, headers={"User-Agent": "cargo"})
		jwks_clients[url] = client

	return client
