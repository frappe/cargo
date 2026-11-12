import tempfile
from pathlib import Path

from frappe.tests import UnitTestCase

from cargo.application_detection.detect import detect_project
from cargo.application_detection.exceptions import EntrypointNotFound
from cargo.application_detection.python.flask import Flask


class UnitTestFlask(UnitTestCase):
	"""Flask detection against small sample projects."""

	def setUp(self):
		self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
		self.write("requirements.txt", "Flask==3.0\n")

	def write(self, path: str, content: str = "") -> None:
		file = self.root / path
		file.parent.mkdir(parents=True, exist_ok=True)
		file.write_text(content)

	def test_aliased_import_and_multiline_call(self):
		self.write("web/app.py", "from flask import Flask as F\n\nserver = F(\n    __name__,\n)\n")
		self.write("tests/conftest.py", "from flask import Flask\napp = Flask(__name__)\n")
		project = detect_project(self.root)
		self.assertIsInstance(project, Flask)
		self.assertEqual(project.start_command, "gunicorn --chdir web 'app:server' --bind 0.0.0.0:8000")
		self.assertIsNone(project.pre_release_command)

	def test_factory_with_migrations(self):
		self.write("requirements.txt", "flask\nFlask-Migrate\n")
		self.write("shop.py", "import flask\n\ndef build():\n    return flask.Flask(__name__)\n")
		project = Flask(self.root)
		self.assertEqual(project.entrypoint, "shop:build()")
		self.assertEqual(project.pre_release_command, "cd . && flask --app 'shop:build()' db upgrade")

	def test_no_match_without_dependency_or_app(self):
		self.write("app.py", '"""app = Flask(__name__)"""\nclass Flask: ...\napp = Flask()\n')
		self.assertFalse(Flask(self.root).matches())

		self.write("requirements.txt", "django\n")
		self.write("app.py", "from flask import Flask\napp = Flask(__name__)\n")
		self.assertFalse(Flask(self.root).matches())

	def test_app_not_assigned(self):
		self.write("app.py", "from flask import Flask\napps = {'main': Flask(__name__)}\n")
		with self.assertRaises(EntrypointNotFound):
			Flask(self.root).start_command
