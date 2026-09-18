# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.password import get_decrypted_password

from cargo.api.webhooks import configure
from cargo.telemetry.doctype.datum_server.datum_server import WEBHOOK_NAME as TELEMETRY_WEBHOOK
from cargo.telemetry.doctype.datum_server.datum_server import configure_telemetry_webhook
from cargo.testing import reset_datum_server, use_test_settings

RECEIVER = "https://central.example.test/api/method/central.api.state_delivery.receive"
SECRET = "a-shared-secret"


class UnitTestWebhookEnrolment(IntegrationTestCase):
	"""What Central hands over, and what Cargo does with it."""

	def setUp(self) -> None:
		super().setUp()
		frappe.set_user("Administrator")
		use_test_settings()
		reset_datum_server()
		self.addCleanup(frappe.db.rollback)

	def enrol(self, **changes) -> dict:
		"""The route as Central calls it, with the token check its own tests cover stood down."""
		payload = {"request_url": RECEIVER, "webhook_secret": SECRET, **changes}
		with patch("cargo.auth.authenticate_request", return_value=frappe._dict()):
			return configure(**payload)

	def telemetry_host(self) -> None:
		"""A host that has reported once, so it has a delivery to repoint."""
		frappe.get_single("Datum Server").update(
			{
				"clickhouse_host": "clickhouse.internal",
				"repository": "https://github.com/frappe/datum",
				"version": "develop",
			}
		).save()

	def test_the_receiver_is_stored_for_every_delivery(self):
		"""One receiver and one secret serve every service, so they live on the settings
		each delivery reads rather than on any one host."""
		self.enrol()

		settings = frappe.get_doc("Cargo Settings")
		self.assertEqual(settings.central_webhook_url, RECEIVER)
		self.assertTrue(settings.central_webhook_enabled)
		self.assertEqual(
			get_decrypted_password("Cargo Settings", "Cargo Settings", "central_webhook_secret"),
			SECRET,
		)

	def test_a_delivery_built_after_enrolment_carries_the_receiver(self):
		self.enrol()
		self.telemetry_host()

		webhook = frappe.get_doc("Webhook", TELEMETRY_WEBHOOK)
		self.assertEqual(webhook.request_url, RECEIVER)
		self.assertEqual(webhook.get_password("webhook_secret"), SECRET)
		self.assertTrue(webhook.enabled)

	def test_a_rotated_secret_reaches_a_delivery_rebuilt_after_it(self):
		"""The route stores; a delivery picks it up when it is next built."""
		self.telemetry_host()
		self.enrol(webhook_secret="rotated")

		configure_telemetry_webhook(frappe.get_single("Datum Server"))

		self.assertEqual(
			frappe.get_doc("Webhook", TELEMETRY_WEBHOOK).get_password("webhook_secret"), "rotated"
		)

	def test_no_delivery_is_created_or_touched(self):
		"""Enrolment is settings only: it knows nothing about which services report."""
		self.enrol()

		self.assertFalse(frappe.db.exists("Webhook", TELEMETRY_WEBHOOK))

	def test_reporting_can_be_turned_off_without_withdrawing_the_receiver(self):
		"""Central stops the reports; the URL and secret stay for when it turns them back on."""
		self.enrol(enabled=False)
		self.telemetry_host()

		self.assertFalse(frappe.get_doc("Webhook", TELEMETRY_WEBHOOK).enabled)
		self.assertEqual(frappe.get_doc("Cargo Settings").central_webhook_url, RECEIVER)

	def test_a_receiver_without_a_url_or_a_secret_is_refused(self):
		"""Storing half of it leaves deliveries that cannot be verified."""
		for missing in ({"request_url": ""}, {"webhook_secret": ""}):
			with self.subTest(missing=missing), self.assertRaises(frappe.ValidationError):
				self.enrol(**missing)

	def test_the_route_is_authenticated(self):
		"""Anyone reaching it could point this region's reports at themselves."""
		with self.assertRaises(frappe.AuthenticationError):
			configure(request_url=RECEIVER, webhook_secret=SECRET)
