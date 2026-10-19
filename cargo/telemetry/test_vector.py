import tempfile
from pathlib import Path

from frappe.tests import IntegrationTestCase

from cargo.telemetry.vector import write_local_credential


class IntegrationTestLocalCredential(IntegrationTestCase):
	"""Cargo's own Vector, which Atlas installed and Cargo hands each new token."""

	def setUp(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.path = Path(directory.name) / "datum.env"

	def test_a_host_atlas_did_not_install_is_left_alone(self):
		self.assertFalse(write_local_credential("https://datum.test", "datum-jwt", path=self.path))
		self.assertFalse(self.path.exists())

	def test_the_token_replaces_the_old_one(self):
		self.path.write_text("DATUM_ENDPOINT=https://old.test\nDATUM_TOKEN=old-jwt\n")

		self.assertTrue(write_local_credential("https://datum.test/", "datum-jwt", path=self.path))
		self.assertEqual(self.path.read_text(), "DATUM_ENDPOINT=https://datum.test\nDATUM_TOKEN=datum-jwt\n")

	def test_the_file_keeps_the_permissions_atlas_gave_it(self):
		self.path.touch(mode=0o600)
		write_local_credential("https://datum.test", "datum-jwt", path=self.path)

		self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
