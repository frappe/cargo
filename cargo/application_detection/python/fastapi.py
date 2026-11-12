import ast
import json
import shlex
import subprocess
from functools import cached_property
from pathlib import Path

from cargo.application_detection.exceptions import EntrypointNotFound
from cargo.application_detection.python import PythonProject

IGNORED_DIRECTORIES = {"tests", "test", "docs", "examples", "scripts"}


class FastAPI(PythonProject):
	dependency_name = "fastapi"
	default_run_port = 8000

	def matches(self) -> bool:
		return self.dependency_name in self.dependencies and bool(self.fastapi_initialization)

	@cached_property
	def fastapi_initialization(self) -> tuple[Path, int] | None:
		"""Shallowest `FastAPI(...)` call outside tests and docs, as (file relative to the root, line)."""
		result = subprocess.run(
			[
				"semgrep",
				"-e",
				"fastapi.FastAPI(...)",
				"--lang",
				"python",
				"--json",
				"-q",
				"--metrics=off",
				str(self.project_tree.path),
			],
			capture_output=True,
			text=True,
			check=True,
		)
		matches = [
			(Path(match["path"]).resolve().relative_to(self.project_tree.path), match["start"]["line"])
			for match in json.loads(result.stdout).get("results", [])
		]
		matches = [match for match in matches if not IGNORED_DIRECTORIES.intersection(match[0].parts)]
		return min(matches, key=lambda match: (len(match[0].parts), match), default=None)

	@property
	def fastapi_initialization_file(self) -> Path:
		if not self.fastapi_initialization:
			raise EntrypointNotFound("No FastAPI() app found. Set run.command in frappe_cloud_deploy.toml.")
		return self.fastapi_initialization[0]

	@property
	def root_path(self) -> str:
		return str(self.fastapi_initialization_file.parent)

	@cached_property
	def app(self) -> tuple[str, bool]:
		"""(name, is_factory) of the variable or top-level function holding the FastAPI() call."""
		line = self.fastapi_initialization[1]
		tree = ast.parse(self.project_tree.readfile(self.fastapi_initialization_file))
		for node in tree.body:
			if not node.lineno <= line <= node.end_lineno:
				continue
			if isinstance(node, ast.Assign | ast.AnnAssign) and isinstance(node.value, ast.Call):
				target = node.targets[0] if isinstance(node, ast.Assign) else node.target
				if isinstance(target, ast.Name):
					return target.id, False
			if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
				return node.name, True
		raise EntrypointNotFound(
			f"FastAPI() in {self.fastapi_initialization_file} is not assigned to a module-level variable "
			"or returned by a top-level function. Set run.command in frappe_cloud_deploy.toml."
		)

	@property
	def entrypoint(self) -> str:
		return f"{self.fastapi_initialization_file.stem}:{self.app[0]}"

	@property
	def pre_release_command(self) -> str | None:
		if (self.path / "alembic.ini").is_file():
			return "alembic upgrade head"
		return None

	@property
	def start_command(self) -> str:
		factory = " --factory" if self.app[1] else ""
		return (
			f"uvicorn --app-dir {shlex.quote(self.root_path)} {self.entrypoint}{factory} "
			f"--host 0.0.0.0 --port {self.default_run_port}"
		)
