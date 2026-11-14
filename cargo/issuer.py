from __future__ import annotations

import time
import typing
from typing import Any
from uuid import uuid4

import frappe
import jwt
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
	Encoding,
	NoEncryption,
	PrivateFormat,
	load_pem_private_key,
)
from frappe import _
from frappe.utils.password import set_encrypted_password
from jwt.algorithms import OKPAlgorithm

from cargo.auth import ALGORITHM

if typing.TYPE_CHECKING:
	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings

SETTINGS = "Cargo Settings"
DATUM_TOKEN_LIFETIME = 7 * 24 * 60 * 60
MACHINE_AUDIENCE = "cargo:machine"
ATLAS_KEYS_CACHE_KEY = "cargo:atlas_jwks"
ATLAS_KEYS_CACHE_SECONDS = 5 * 60


def initialize_signing_key() -> None:
	"""Create the Ed25519 key that Cargo signs Datum and machine tokens with, once for its region.
	Runs from `after_install` and a migrate patch, so two workers never race to create it."""
	settings: CargoSettings = frappe.get_single(SETTINGS)
	if settings.get_password("signing_private_key", raise_exception=False) and has_region_key_id(settings):
		return

	private_key = Ed25519PrivateKey.generate().private_bytes(
		Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
	)
	# Written field by field: a Password reads back empty, so saving the whole Single fails.
	set_encrypted_password(SETTINGS, SETTINGS, private_key.decode(), "signing_private_key")
	frappe.db.set_single_value(SETTINGS, "signing_key_id", f"{get_issuer(settings.region_id)}:{uuid4()}")
	frappe.clear_document_cache(SETTINGS, SETTINGS)


def get_signing_key() -> tuple[Ed25519PrivateKey, str]:
	"""The private key and key id of this region's Cargo issuer."""
	settings: CargoSettings = frappe.get_cached_doc(SETTINGS)
	private_key = settings.get_password("signing_private_key", raise_exception=False)
	if not private_key or not has_region_key_id(settings):
		frappe.throw(_("Cargo has no signing key for its region. Run bench migrate."))

	key = load_pem_private_key(private_key.encode(), password=None)
	if not isinstance(key, Ed25519PrivateKey):
		frappe.throw(_("The Cargo signing key must use Ed25519."))

	return key, settings.signing_key_id


def has_region_key_id(settings: CargoSettings) -> bool:
	prefix = f"{get_issuer(settings.region_id)}:"
	return (settings.signing_key_id or "").startswith(prefix) and settings.signing_key_id != prefix


def get_issuer(region_id: int) -> str:
	return f"cargo:{region_id}"


def get_public_jwk() -> dict[str, Any]:
	key, key_id = get_signing_key()
	jwk = OKPAlgorithm.to_jwk(key.public_key(), as_dict=True)
	jwk.update({"alg": ALGORITHM, "kid": key_id, "use": "sig"})
	return jwk


def get_merged_jwks() -> dict[str, list]:
	"""The Atlas key set, which carries Central's keys too, and Cargo's own key. Raises when Atlas
	does not answer, so a reader keeps the set it has rather than lose the Atlas keys."""
	atlas_keys = frappe.cache.get_value(ATLAS_KEYS_CACHE_KEY)
	if atlas_keys is None:
		settings: CargoSettings = frappe.get_cached_doc(SETTINGS)
		response = requests.get(settings.jwks_url, headers={"User-Agent": "cargo"}, timeout=(5, 10))
		response.raise_for_status()
		atlas_keys = response.json().get("keys")
		if not isinstance(atlas_keys, list):
			raise ValueError(f"The key set at {settings.jwks_url} has no keys list")

		frappe.cache.set_value(ATLAS_KEYS_CACHE_KEY, atlas_keys, expires_in_sec=ATLAS_KEYS_CACHE_SECONDS)

	return {"keys": [*atlas_keys, get_public_jwk()]}


def issue_datum_token(resource_id: str) -> str:
	"""A token that writes to this region's Datum as `resource_id`."""
	region_id = frappe.get_cached_doc(SETTINGS).region_id
	return issue_token(
		f"atlas-datum:{region_id}",
		{"resource_id": resource_id, "access": ["write"], "scope": "datum"},
		lifetime=DATUM_TOKEN_LIFETIME,
	)


def generate_machine_token(machine: str, virtual_machine: str) -> str:
	"""The credential a machine trades with this Cargo for its own Datum token. It has no expiry: it
	stays in the machine's metadata, and Cargo refuses it once the Machine is not Running."""
	return issue_token(MACHINE_AUDIENCE, {"sub": machine, "virtual_machine": virtual_machine})


def verify_machine_token(token: str) -> str:
	"""The Atlas Virtual Machine of the Running Machine that this machine token names."""
	key, _key_id = get_signing_key()
	region_id = frappe.get_cached_doc(SETTINGS).region_id
	try:
		claims = jwt.decode(
			token,
			key.public_key(),
			algorithms=[ALGORITHM],
			audience=MACHINE_AUDIENCE,
			issuer=get_issuer(region_id),
			options={"require": ["iss", "sub", "aud", "iat"]},
		)
	except jwt.PyJWTError:
		frappe.throw(_("Invalid machine token."), frappe.AuthenticationError)

	machine = frappe.db.get_value("Machine", claims["sub"], ["status", "vm_id"], as_dict=True)
	if (
		not machine
		or machine.status != "Running"
		or not machine.vm_id
		or machine.vm_id != claims.get("virtual_machine")
	):
		frappe.throw(_("Invalid machine token."), frappe.AuthenticationError)

	return machine.vm_id


def issue_token(audience: str, claims: dict[str, Any], lifetime: int | None = None) -> str:
	key, key_id = get_signing_key()
	region_id = frappe.get_cached_doc(SETTINGS).region_id
	now = int(time.time())
	payload = {
		"iss": get_issuer(region_id),
		"sub": "cargo",
		"aud": audience,
		"iat": now,
		"jti": frappe.generate_hash(length=16),
		**claims,
	}
	if lifetime:
		payload["exp"] = now + lifetime

	return jwt.encode(payload, key, algorithm=ALGORITHM, headers={"kid": key_id})
