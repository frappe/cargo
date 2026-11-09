import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import jwt
from frappe.tests import UnitTestCase

from cargo.telemetry import credential
from cargo.telemetry.credential import renew_datum_token

ENDPOINT = "https://telemetry-svc.example.test"
SETTINGS = SimpleNamespace(central_url="https://central.test/", region="blr")


def datum_token(expires_in: int) -> str:
	return jwt.encode({"exp": int(time.time()) + expires_in}, "a-secret-of-thirty-two-bytes-long", "HS256")


class UnitTestRenewDatumToken(UnitTestCase):
	"""The Cargo host's Pilot has no Central credential, so Cargo renews its own Datum token."""

	def renew(self, current: str | None, status: str = "Active") -> tuple[Mock, Mock, Mock]:
		server = SimpleNamespace(status=status, service_endpoint=ENDPOINT)
		info = (
			{"return_value": {"endpoint": ENDPOINT, "token": current}}
			if current
			else {"side_effect": RuntimeError}
		)
		answer = Mock(**{"json.return_value": {"message": {"token": "new-datum-token"}}})
		with (
			patch.object(credential.frappe, "get_single", return_value=server),
			patch.object(credential.frappe, "get_cached_doc", return_value=SETTINGS),
			patch.object(credential, "get_metrics_info", **info),
			patch.object(credential, "generate_machine_token", return_value="machine-token") as generate,
			patch.object(credential.requests, "get", return_value=answer) as get,
			patch.object(credential, "write_telemetry_credential") as write,
		):
			renew_datum_token()

		return generate, get, write

	def test_a_fresh_token_is_kept(self):
		_, get, write = self.renew(datum_token(expires_in=5 * 24 * 60 * 60))

		get.assert_not_called()
		write.assert_not_called()

	def test_an_expiring_token_is_replaced_by_one_from_central(self):
		generate, get, write = self.renew(datum_token(expires_in=60 * 60))

		generate.assert_called_once_with("cargo", "blr-cargo-server")
		self.assertEqual(
			get.call_args.args[0], "https://central.test/api/method/central.api.cargo.datum_token"
		)
		self.assertEqual(get.call_args.kwargs["headers"]["X-Cargo-Machine-Token"], "machine-token")
		write.assert_called_once_with(ENDPOINT, "new-datum-token")

	def test_a_host_with_no_token_gets_one(self):
		_, _, write = self.renew(None)

		write.assert_called_once_with(ENDPOINT, "new-datum-token")

	def test_nothing_is_fetched_until_the_regions_datum_host_is_active(self):
		_, get, write = self.renew(None, status="Setting Up")

		get.assert_not_called()
		write.assert_not_called()
