import shlex
from functools import cached_property
from pathlib import Path

from cargo.application_detection.exceptions import EntrypointNotFound
from cargo.application_detection.python import AppFind, ImportPath, PythonProject, SemgrepFind


class Flask(PythonProject):
	dependency_name = "flask"
	default_run_port = 8000
	server_dependency = "gunicorn"

	def matches(self) -> bool:
		return self.dependency_name in self.dependencies and bool(self.flask_initialization)

	@cached_property
	def flask_initialization(self) -> SemgrepFind | None:
		return self.find_call("flask.Flask(...)")

	@property
	def flask_initialization_file(self) -> Path:
		if not self.flask_initialization:
			raise EntrypointNotFound("No Flask() app found. Set run.command in frappe_cloud_deploy.toml.")
		return self.flask_initialization.filepath

	@property
	def root_path(self) -> str:
		return str(self.import_path.root)

	@cached_property
	def import_path(self) -> ImportPath:
		return self.get_import_path(self.flask_initialization_file)

	@cached_property
	def app(self) -> AppFind:
		return self.get_app(self.flask_initialization)

	@property
	def entrypoint(self) -> str:
		"""`module:app`, or `module:create_app()` for a factory, which both gunicorn and flask accept."""
		call = "()" if self.app.is_factory else ""
		return f"{self.import_path.module}:{self.app.app_name}{call}"

	@property
	def pre_release_command(self) -> str | None:
		if "flask-migrate" in self.dependencies:
			return f"cd {shlex.quote(self.root_path)} && flask --app '{self.entrypoint}' db upgrade"
		return None

	@property
	def start_command(self) -> str:
		return (
			f"gunicorn --chdir {shlex.quote(self.root_path)} '{self.entrypoint}' "
			f"--bind 0.0.0.0:{self.default_run_port}"
		)
