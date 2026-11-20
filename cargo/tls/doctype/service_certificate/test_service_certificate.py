from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import frappe
from cloudtoolkit.tls.letsencrypt import IssuedCertificate
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, get_datetime, now_datetime

from cargo.testing import use_test_settings
from cargo.tls.doctype.service_certificate.service_certificate import (
	get_account_key,
	get_service_certificate,
	get_serving_proxy,
	renew_due_certificates,
)

MODULE = "cargo.tls.doctype.service_certificate.service_certificate"
DOMAINS = ("s3-svc.example.test", "s3-admin-svc.example.test")
ISSUED = IssuedCertificate(
	certificate_pem="certificate-pem",
	private_key_pem="private-key-pem",
	expires_on=datetime(2027, 2, 18, tzinfo=UTC),
)


class IntegrationTestServiceCertificate(IntegrationTestCase):
	"""One Let's Encrypt certificate per service, issued on install and renewed before it expires."""

	def setUp(self):
		use_test_settings()
		frappe.db.delete("Service Certificate")

	def issue(self, *args, **kwargs):
		with patch(f"{MODULE}.LetsEncrypt") as letsencrypt:
			letsencrypt.return_value.issue_certificate.return_value = ISSUED
			certificate = get_service_certificate(*args, **kwargs)
		return certificate, letsencrypt.return_value.issue_certificate

	def add_certificate(self, name: str, expires_in_days: int):
		return frappe.get_doc(
			{
				"doctype": "Service Certificate",
				"name": name,
				"domains": "\n".join(DOMAINS),
				"status": "Active",
				"expires_on": add_to_date(now_datetime(), days=expires_in_days),
			}
		).insert()

	def test_a_new_certificate_is_issued_for_the_service_domains(self):
		certificate, issued = self.issue("object-storage", DOMAINS)

		issued.assert_called_once_with(list(DOMAINS))
		certificate.reload()
		self.assertEqual(certificate.status, "Active")
		self.assertEqual(certificate.certificate, "certificate-pem")
		self.assertEqual(certificate.get_password("private_key"), "private-key-pem")
		self.assertEqual(certificate.dns_names, list(DOMAINS))
		self.assertIsNotNone(certificate.expires_on)

	def test_a_certificate_far_from_expiry_is_not_issued_again(self):
		self.add_certificate("object-storage", expires_in_days=60)

		_, issued = self.issue("object-storage", DOMAINS)

		issued.assert_not_called()

	def test_new_domains_issue_the_certificate_again(self):
		self.add_certificate("object-storage", expires_in_days=60)

		certificate, issued = self.issue("object-storage", ("s3-svc.example.test",))

		issued.assert_called_once_with(["s3-svc.example.test"])
		self.assertEqual(certificate.dns_names, ["s3-svc.example.test"])

	def test_a_certificate_is_due_within_thirty_days_of_expiry(self):
		self.assertTrue(self.add_certificate("object-storage", expires_in_days=10).is_due)
		self.assertFalse(self.add_certificate("telemetry", expires_in_days=60).is_due)

	def test_a_failed_issue_is_kept_as_failed(self):
		with (
			patch(f"{MODULE}.LetsEncrypt") as letsencrypt,
			self.assertRaisesRegex(RuntimeError, "acme refused"),
		):
			letsencrypt.return_value.issue_certificate.side_effect = RuntimeError("acme refused")
			get_service_certificate("object-storage", DOMAINS)

		self.assertEqual(frappe.db.get_value("Service Certificate", "object-storage", "status"), "Failed")

	def test_the_issued_expiry_is_stored_in_system_time(self):
		certificate, _ = self.issue("object-storage", DOMAINS)

		stored = get_datetime(certificate.expires_on)
		self.assertLess(abs(stored - get_datetime("2027-02-18")), timedelta(days=1))

	def test_the_account_key_is_created_once_and_kept(self):
		frappe.db.set_single_value("Cargo Settings", "letsencrypt_account_key", "")
		frappe.db.delete("__Auth", {"doctype": "Cargo Settings", "fieldname": "letsencrypt_account_key"})

		first = get_account_key(frappe.get_single("Cargo Settings"))
		second = get_account_key(frappe.get_single("Cargo Settings"))

		self.assertIn("PRIVATE KEY", first)
		self.assertEqual(first, second)

	def test_renewal_puts_a_due_certificate_on_its_serving_service(self):
		self.add_certificate("object-storage", expires_in_days=10)
		self.add_certificate("telemetry", expires_in_days=60)
		proxy = Mock()

		with patch(f"{MODULE}.get_serving_proxy", return_value=proxy) as serving:
			renew_due_certificates()

		serving.assert_called_once_with("object-storage")
		proxy.renew_certificate.assert_called_once_with()

	def test_one_failed_renewal_is_logged_and_the_rest_still_renew(self):
		self.add_certificate("object-storage", expires_in_days=10)
		self.add_certificate("telemetry", expires_in_days=10)
		proxy = Mock(**{"renew_certificate.side_effect": [RuntimeError("acme refused"), None]})

		with (
			patch(f"{MODULE}.get_serving_proxy", return_value=proxy),
			patch("frappe.log_error") as logged,
		):
			renew_due_certificates()

		self.assertEqual(proxy.renew_certificate.call_count, 2)
		logged.assert_called_once()

	def test_a_certificate_no_service_serves_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "No service serves"):
			get_serving_proxy("mail")
