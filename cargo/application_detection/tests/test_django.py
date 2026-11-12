import tempfile
from pathlib import Path

from frappe.tests import UnitTestCase

from cargo.application_detection.detect import detect_project
from cargo.application_detection.exceptions import (
	EntrypointNotFound,
	NoSupportedFrameworksFound,
	PathOutsideProject,
)
from cargo.application_detection.python.django import Django

SETTINGS = 'SECRET_KEY = "x"\nWSGI_APPLICATION = "{}"\n'


class UnitTestDjango(UnitTestCase):
	"""Django detection against small sample projects."""

	def setUp(self):
		self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

	def write(self, path: str, content: str = "") -> None:
		file = self.root / path
		file.parent.mkdir(parents=True, exist_ok=True)
		file.write_text(content)

	def test_standard_layout(self):
		self.write("requirements.txt", "Django==5.1\n")
		self.write("manage.py")
		self.write("shop/settings.py", SETTINGS.format("shop.wsgi.application"))
		project = Django(self.root)
		self.assertTrue(project.matches())
		self.assertEqual(project.root_path, ".")
		self.assertEqual(
			project.start_command, "gunicorn --chdir . shop.wsgi:application --bind 0.0.0.0:8000"
		)

	def test_nested_project_with_annotated_settings(self):
		self.write("pyproject.toml", '[project]\ndependencies = ["django>=5"]\n')
		self.write("src/manage.py")
		self.write(
			"src/config/settings.py",
			'SECRET_KEY: str = os.environ["SECRET_KEY"]\nWSGI_APPLICATION: str = "config.wsgi.app"\n',
		)
		project = Django(self.root)
		self.assertTrue(project.matches())
		self.assertEqual(project.root_path, "src")
		self.assertEqual(project.entrypoint, "config.wsgi:app")

	def test_does_not_match_without_secret_key_or_dependency(self):
		self.write("requirements.txt", "django\n")
		self.write("manage.py")
		self.write("shop/settings.py", '# SECRET_KEY = "x"\ndef f():\n    SECRET_KEY = "x"\n')
		self.assertFalse(Django(self.root).matches())

		self.write("requirements.txt", "flask\n")
		self.write("shop/settings.py", SETTINGS.format("shop.wsgi.application"))
		self.assertFalse(Django(self.root).matches())

	def test_missing_or_invalid_wsgi_application(self):
		self.write("manage.py")
		for settings in (
			'SECRET_KEY = "x"\n',
			SETTINGS.format("application"),
			SETTINGS.format("a.b; rm -rf /"),
		):
			self.write("shop/settings.py", settings)
			with self.assertRaises(EntrypointNotFound):
				Django(self.root).entrypoint

	def test_refuses_symlink_outside_project(self):
		outside = Path(self.enterContext(tempfile.TemporaryDirectory())) / "secret"
		outside.write_text("django\n")
		(self.root / "requirements.txt").symlink_to(outside)
		with self.assertRaises(PathOutsideProject):
			Django(self.root).dependencies

	def test_detect_project(self):
		self.write("requirements.txt", "django\n")
		self.write("manage.py")
		self.write("shop/settings.py", SETTINGS.format("shop.wsgi.application"))
		self.assertIsInstance(detect_project(self.root), Django)

		self.write("requirements.txt", "flask\n")
		with self.assertRaises(NoSupportedFrameworksFound):
			detect_project(self.root)

	def test_declared_build_system_skips_detection(self):
		self.write("frappe_cloud_deploy.toml")
		self.write("Dockerfile")
		self.assertIsNone(detect_project(self.root))
