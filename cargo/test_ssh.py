from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.image_builder.doctype.pilot_image.pilot_image import PilotImage
from cargo.ssh import OutputLog, live_output


class TestOutputLog(IntegrationTestCase):
	"""How a command's output reaches its document, while it runs and once it ends."""

	def image(self) -> PilotImage:
		with patch.object(PilotImage, "after_insert"):
			image: PilotImage = frappe.get_doc(
				{
					"doctype": "Pilot Image",
					"pilot_version": f"v0.0.1-{frappe.generate_hash(length=6)}",
					"frappe_branch": "version-16",
					"image_type": "Base",
				}
			).insert()

		image.db_set("build_log", "the last run's log")
		return image

	def test_a_run_does_not_write_the_document_until_it_ends(self):
		"""A write would lock the row for the whole run, and block a stop request."""
		image = self.image()

		with patch.object(PilotImage, "db_set") as db_set, OutputLog(image, "build_log") as log:
			log.write("step one\n")
			db_set.assert_not_called()

		db_set.assert_called_once_with("build_log", "step one\n", update_modified=False)

	def test_a_new_run_hides_the_last_runs_log(self):
		image = self.image()

		with OutputLog(image, "build_log"):
			self.assertEqual(live_output(image, "build_log"), "")

	def test_a_run_that_prints_nothing_clears_the_last_runs_log(self):
		image = self.image()

		with OutputLog(image, "build_log"):
			pass

		self.assertEqual(frappe.db.get_value("Pilot Image", image.name, "build_log"), "")
