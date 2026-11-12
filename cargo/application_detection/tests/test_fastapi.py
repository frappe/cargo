import tempfile
from pathlib import Path

from frappe.tests import UnitTestCase

from cargo.application_detection.detect import detect_project
from cargo.application_detection.exceptions import EntrypointNotFound
from cargo.application_detection.python.fastapi import FastAPI


class UnitTestFastAPI(UnitTestCase):
	"""FastAPI detection against small sample projects."""

	def setUp(self):
		self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
		self.write("requirements.txt", "fastapi\n")

	def write(self, path: str, content: str = "") -> None:
		file = self.root / path
		file.parent.mkdir(parents=True, exist_ok=True)
		file.write_text(content)

	def test_aliased_import_and_multiline_call(self):
		self.write("app/main.py", 'from fastapi import FastAPI as F\n\napi = F(\n    title="shop",\n)\n')
		self.write("tests/conftest.py", "from fastapi import FastAPI\napp = FastAPI()\n")
		project = detect_project(self.root)
		self.assertIsInstance(project, FastAPI)
		self.assertEqual(project.start_command, "uvicorn --app-dir app main:api --host 0.0.0.0 --port 8000")
		self.assertIsNone(project.pre_release_command)

	def test_annotated_module_import_and_alembic(self):
		self.write("alembic.ini")
		self.write("main.py", "import fastapi\napp: fastapi.FastAPI = fastapi.FastAPI()\n")
		project = FastAPI(self.root)
		self.assertTrue(project.matches())
		self.assertEqual(project.entrypoint, "main:app")
		self.assertEqual(project.pre_release_command, "alembic upgrade head")

	def test_factory(self):
		self.write("server.py", "from fastapi import FastAPI\n\ndef create_app():\n    return FastAPI()\n")
		self.assertIn("server:create_app --factory", FastAPI(self.root).start_command)

	def test_no_match_without_dependency_or_app(self):
		self.write("main.py", '"""app = FastAPI()"""\nclass FastAPI: ...\napp = FastAPI()\n')
		self.assertFalse(FastAPI(self.root).matches())

		self.write("requirements.txt", "flask\n")
		self.write("main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
		self.assertFalse(FastAPI(self.root).matches())

	def test_app_not_assigned(self):
		self.write("main.py", "from fastapi import FastAPI\napps = [FastAPI()]\n")
		with self.assertRaises(EntrypointNotFound):
			FastAPI(self.root).entrypoint

	def test_app_in_package_module(self):
		self.write("src/api/__init__.py")
		self.write("src/api/main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
		self.assertEqual(
			FastAPI(self.root).start_command, "uvicorn --app-dir src api.main:app --host 0.0.0.0 --port 8000"
		)
