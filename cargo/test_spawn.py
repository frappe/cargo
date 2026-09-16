# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.password import remove_encrypted_password
from frappe.utils.synchronization import filelock

from cargo.spawn import (
	REQUIRED_SECRETS,
	REQUIRED_SETTINGS,
	has_required_settings,
	spawn_config,
	spawn_lock,
)
from cargo.testing import use_test_settings

KEY = "test_spawn_config"
LOCK_NAME = "test-spawn"


def refuse(config: dict) -> None:
	raise frappe.ValidationError("a config spawn_config must reject")


def accept(config: dict) -> None:
	return None


class IntegrationTestSpawnConfig(IntegrationTestCase):
	"""What a spawner is told to build, out of site config."""

	def configured(self, config):
		return patch.dict(frappe.local.conf, {KEY: config})

	def test_a_host_told_nothing_builds_nothing(self):
		with self.configured(None):
			self.assertIsNone(spawn_config(KEY, accept))

	def test_a_config_that_cannot_be_used_builds_nothing(self):
		with self.configured({"anything": 1}), patch.object(frappe, "log_error") as logged:
			self.assertIsNone(spawn_config(KEY, refuse))

		self.assertIn(KEY, logged.call_args.kwargs["title"])

	def test_a_usable_config_is_handed_back(self):
		with self.configured({"storage_node_count": 1}):
			self.assertEqual(spawn_config(KEY, accept), {"storage_node_count": 1})


class IntegrationTestRequiredSettings(IntegrationTestCase):
	"""A half provisioned host builds nothing: it is still being installed."""

	def setUp(self):
		frappe.set_user("Administrator")
		use_test_settings()

	def forget(self, field: str) -> None:
		frappe.db.set_single_value("Cargo Settings", field, "")
		frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

	def test_a_fully_provisioned_host_may_build(self):
		self.assertTrue(has_required_settings())

	def test_a_missing_setting_stops_it(self):
		for field in REQUIRED_SETTINGS:
			with self.subTest(field=field):
				use_test_settings()
				self.forget(field)

				self.assertFalse(has_required_settings())

	def test_a_missing_secret_stops_it(self):
		"""A Password field reads back empty from the document, so these are asked for by
		name -- the check would pass blind if they were counted with the rest."""
		for field in REQUIRED_SECRETS:
			with self.subTest(field=field):
				use_test_settings()
				remove_encrypted_password("Cargo Settings", "Cargo Settings", field)
				frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

				self.assertFalse(has_required_settings())


class IntegrationTestSpawnLock(IntegrationTestCase):
	"""Renting a machine is the expensive half, so two runs must not overlap."""

	def test_a_second_run_does_not_wait_for_the_first(self):
		with spawn_lock(LOCK_NAME), self.assertRaises(LockTimeoutError):
			with spawn_lock(LOCK_NAME):
				pass

	def test_the_lock_is_the_site_own_not_the_whole_bench(self):
		"""Two sites are two regions. A bench-wide lock would have one hold up the other, and
		would no longer collide with the site lock a caller takes by name."""
		with filelock(LOCK_NAME, timeout=0), self.assertRaises(LockTimeoutError):
			with spawn_lock(LOCK_NAME):
				pass

	def test_another_spawner_is_not_blocked(self):
		with spawn_lock(LOCK_NAME), spawn_lock(f"{LOCK_NAME}-other"):
			pass
