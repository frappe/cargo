# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from cargo.image_builder.releases import latest_pilot_release


def response(payload) -> Mock:
	answer = Mock(status_code=200)
	answer.json.return_value = payload

	return answer


class UnitTestReleases(UnitTestCase):
	def get(self, answer: Mock):
		return patch("cargo.image_builder.releases.requests.get", return_value=answer)

	def test_the_newest_prerelease_is_the_latest_release(self):
		payload = [
			{"tag_name": "v0.0.32-pre-alpha", "draft": False, "prerelease": True},
			{"tag_name": "v0.0.31-pre-alpha", "draft": False, "prerelease": True},
		]
		with self.get(response(payload)):
			self.assertEqual(latest_pilot_release(), "v0.0.32-pre-alpha")

	def test_a_draft_is_skipped(self):
		payload = [
			{"tag_name": "v0.0.33-pre-alpha", "draft": True, "prerelease": True},
			{"tag_name": "v0.0.32-pre-alpha", "draft": False, "prerelease": True},
		]
		with self.get(response(payload)):
			self.assertEqual(latest_pilot_release(), "v0.0.32-pre-alpha")
