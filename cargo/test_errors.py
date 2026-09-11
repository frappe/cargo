# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

import pickle

from frappe.tests import UnitTestCase

from cargo.atlas_client import AtlasError
from cargo.object_storage.garage.client import Error
from cargo.ssh import SshError

#: Every error a task can raise. The workflow engine pickles them to carry them back.
ERRORS = (AtlasError, Error, SshError)


class UnitTestErrors(UnitTestCase):
	def test_errors_survive_a_pickle_round_trip(self):
		for error in ERRORS:
			with self.subTest(error=error.__name__):
				restored = pickle.loads(pickle.dumps(error("it broke")))

				self.assertIsInstance(restored, error)
				self.assertEqual(str(restored), "it broke")
