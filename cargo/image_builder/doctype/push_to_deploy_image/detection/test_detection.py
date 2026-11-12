import tempfile
from pathlib import Path

from frappe.tests import UnitTestCase

from cargo.image_builder.doctype.push_to_deploy_image.detection import DetectionError, get_project

FLASK_APP = "from flask import Flask\napp = Flask(__name__)\n"


class UnitTestDetection(UnitTestCase):
	"""Project detection against small sample repositories."""

	def setUp(self):
		self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

	def write(self, path: str, content: str) -> None:
		file = self.root / path
		file.parent.mkdir(parents=True, exist_ok=True)
		file.write_text(content)

	def test_django_reads_settings_module_from_manage_py(self):
		self.write("requirements.txt", "Django==5.1\n")
		self.write(
			"src/manage.py", 'os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")\n'
		)
		self.write("src/config/wsgi.py", "")
		project = get_project(self.root)
		self.assertEqual(project.framework, "django")
		self.assertEqual(project.working_directory, "src")
		self.assertIn("gunicorn config.wsgi:application", project.start_command)
		self.assertEqual(project.release_command, "python manage.py migrate --noinput")

	def test_flask_instance(self):
		self.write("requirements.txt", "Flask==3.0\n")
		self.write("app.py", FLASK_APP)
		project = get_project(self.root)
		self.assertEqual(project.framework, "flask")
		self.assertIn("gunicorn 'app:app'", project.start_command)
		self.assertIsNone(project.release_command)

	def test_flask_alias_and_multiline_call(self):
		self.write("requirements.txt", "flask\n")
		self.write("wsgi.py", "from flask import Flask as F\nserver = F(\n    __name__,\n)\n")
		self.assertIn("'wsgi:server'", get_project(self.root).start_command)

	def test_flask_factory_with_any_name_and_migrations(self):
		self.write("requirements.txt", "flask\nFlask-Migrate\n")
		self.write("shop/__init__.py", "import flask\n\ndef build():\n    return flask.Flask(__name__)\n")
		project = get_project(self.root)
		self.assertIn("'shop:build()'", project.start_command)
		self.assertEqual(project.release_command, "flask --app 'shop:build()' db upgrade")

	def test_flask_ignores_docstrings_local_classes_and_tests(self):
		self.write("requirements.txt", "flask\n")
		self.write("app.py", '"""app = Flask(__name__)"""\nclass Flask: ...\napp = Flask()\n')
		self.write("tests/app.py", FLASK_APP)
		with self.assertRaisesRegex(DetectionError, "No Flask\\(\\) app found"):
			get_project(self.root)

	def test_flask_skips_files_that_do_not_parse(self):
		self.write("requirements.txt", "flask\n")
		self.write("app.py", "def broken(:\n")
		self.write("src/web/wsgi.py", FLASK_APP)
		project = get_project(self.root)
		self.assertEqual(project.working_directory, "src")
		self.assertIn("'web.wsgi:app'", project.start_command)

	def test_fastapi_module_import_and_alembic(self):
		self.write("pyproject.toml", '[project]\nname = "api"\ndependencies = ["fastapi>=0.110"]\n')
		self.write("alembic.ini", "")
		self.write("app/main.py", "import fastapi\napi: fastapi.FastAPI = fastapi.FastAPI()\n")
		project = get_project(self.root)
		self.assertEqual(project.framework, "fastapi")
		self.assertIn("uvicorn app.main:api --host", project.start_command)
		self.assertEqual(project.release_command, "alembic upgrade head")

	def test_fastapi_factory(self):
		self.write("requirements.txt", "fastapi\n")
		self.write("main.py", "from fastapi import FastAPI\n\ndef create_app():\n    return FastAPI()\n")
		self.assertIn("uvicorn main:create_app --factory", get_project(self.root).start_command)

	def test_no_supported_framework(self):
		self.write("requirements.txt", "requests\n")
		with self.assertRaisesRegex(DetectionError, "No supported framework found"):
			get_project(self.root)

	def test_config_file_skips_detection_and_reports_missing_fields(self):
		self.write("requirements.txt", "flask\n")
		self.write("app.py", FLASK_APP)
		self.write(
			"frappe_cloud_deploy.toml", '[app]\nlanguage = "python"\nversion = "3.12"\nframework = "flask"\n'
		)
		with self.assertRaisesRegex(DetectionError, "missing: run.command, run.port"):
			get_project(self.root)
