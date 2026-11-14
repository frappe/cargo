import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import jwt
from frappe.tests import UnitTestCase

from cargo.telemetry import credential
from cargo.telemetry.credential import get_application_credential, renew_datum_token

ENDPOINT = "https://telemetry-svc.example.test"


def datum_token(expires_in: int) -> str:
	return jwt.encode({"exp": int(time.time()) + expires_in}, "a-secret-of-thirty-two-bytes-long", "HS256")


class UnitTestRenewDatumToken(UnitTestCase):
	"""Cargo issues the Datum token that the Pilot of its own VM sends with."""

	def renew(self, current: str | None, status: str = "Active") -> tuple[Mock, Mock]:
		server = SimpleNamespace(status=status, service_endpoint=ENDPOINT)
		info = (
			{"return_value": {"endpoint": ENDPOINT, "token": current}}
			if current
			else {"side_effect": RuntimeError}
		)
		with (
			patch.object(credential.frappe, "get_single", return_value=server),
			patch.object(credential, "get_metrics_info", **info),
			patch.object(credential, "issue_datum_token", return_value="new-datum-token") as issue,
			patch.object(credential, "get_virtual_machine_name", return_value="vm-cargo"),
			patch.object(credential, "write_telemetry_credential") as write,
		):
			renew_datum_token()

		return issue, write

	def test_a_fresh_token_is_kept(self):
		issue, write = self.renew(datum_token(expires_in=5 * 24 * 60 * 60))

		issue.assert_not_called()
		write.assert_not_called()

	def test_an_expiring_token_is_replaced_by_one_for_the_virtual_machine(self):
		issue, write = self.renew(datum_token(expires_in=60 * 60))

		issue.assert_called_once_with("vm-cargo")
		write.assert_called_once_with(ENDPOINT, "new-datum-token")

	def test_a_host_with_no_token_gets_one(self):
		_, write = self.renew(None)

		write.assert_called_once_with(ENDPOINT, "new-datum-token")

	def test_nothing_is_issued_until_the_regions_datum_host_is_active(self):
		issue, write = self.renew(None, status="Setting Up")

		issue.assert_not_called()
		write.assert_not_called()


class UnitTestApplicationCredential(UnitTestCase):
	"""The Cargo site sends its own metrics and Error Logs as resource `cargo`, with a token apart from Pilot's."""

	def get(self, cached: str | None, status: str = "Active") -> tuple[dict | None, Mock, Mock]:
		server = SimpleNamespace(status=status, service_endpoint=ENDPOINT)
		with (
			patch.object(credential.frappe, "get_single", return_value=server),
			patch.object(credential.frappe.cache, "get_value", return_value=cached),
			patch.object(credential.frappe.cache, "set_value") as store,
			patch.object(credential, "issue_datum_token", return_value="new-datum-token") as issue,
		):
			try:
				info = get_application_credential()
			except RuntimeError:
				info = None

		return info, issue, store

	def test_a_cached_token_is_used(self):
		token = datum_token(expires_in=5 * 24 * 60 * 60)
		info, issue, store = self.get(token)

		self.assertEqual(info, {"endpoint": ENDPOINT, "token": token})
		issue.assert_not_called()
		store.assert_not_called()

	def test_a_missing_token_is_issued_as_cargo(self):
		info, issue, store = self.get(None)

		self.assertEqual(info["token"], "new-datum-token")
		issue.assert_called_once_with("cargo")
		store.assert_called_once_with(credential.APPLICATION_TOKEN_KEY, "new-datum-token")

	def test_nothing_is_sent_until_the_regions_datum_host_is_active(self):
		info, issue, _ = self.get(None, status="Setting Up")

		self.assertIsNone(info)
		issue.assert_not_called()
