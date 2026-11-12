import shlex
from functools import cached_property
from pathlib import Path

from cargo.application_detection.exceptions import EntrypointNotFound
from cargo.application_detection.python import AppFind, PythonProject, SemgrepFind


class FastAPI(PythonProject):
	dependency_name = "fastapi"
	default_run_port = 8000

	def matches(self) -> bool:
		return self.dependency_name in self.dependencies and bool(self.fastapi_initialization)

	@cached_property
	def fastapi_initialization(self) -> SemgrepFind | None:
		return self.find_call("fastapi.FastAPI(...)")

	@property
	def fastapi_initialization_file(self) -> Path:
		if not self.fastapi_initialization:
			raise EntrypointNotFound("No FastAPI() app found. Set run.command in frappe_cloud_deploy.toml.")
		return self.fastapi_initialization.filepath

	@property
	def root_path(self) -> str:
		return str(self.fastapi_initialization_file.parent)

	@cached_property
	def app(self) -> AppFind:
		return self.get_app(self.fastapi_initialization)

	@property
	def entrypoint(self) -> str:
		return f"{self.fastapi_initialization_file.stem}:{self.app.app_name}"

	@property
	def pre_release_command(self) -> str | None:
		if (self.path / "alembic.ini").is_file():
			return "alembic upgrade head"
		return None

	@property
	def start_command(self) -> str:
		factory = " --factory" if self.app.is_factory else ""
		return (
			f"uvicorn --app-dir {shlex.quote(self.root_path)} {self.entrypoint}{factory} "
			f"--host 0.0.0.0 --port {self.default_run_port}"
		)
