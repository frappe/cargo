import ast
import json
import re
import shlex
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

import jinja2
from poetry.core.constraints.version import Version, parse_constraint

from cargo.application_detection import BaseProject
from cargo.application_detection.exceptions import EntrypointNotFound

REQUIREMENT_NAME_PATTERN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
IGNORED_DIRECTORIES = {"tests", "test", "docs", "examples", "scripts"}
DEFAULT_PYTHON_VERSION = "3.12"
SUPPORTED_PYTHON_VERSIONS = ("3.14", "3.13", "3.12", "3.11", "3.10")
TEMPLATES = jinja2.Environment(
	loader=jinja2.FileSystemLoader(Path(__file__).parent),
	undefined=jinja2.StrictUndefined,
	keep_trailing_newline=True,
	trim_blocks=True,
	lstrip_blocks=True,
)
PYTHON_VERSION_PATTERN = re.compile(r"3\.\d+(\.\d+)?")


@dataclass
class SemgrepFind:
	filepath: Path
	lineno: int


@dataclass
class AppFind:
	app_name: str  # Variable that stores the class initialization
	is_factory: bool  # Initiated via a function


@dataclass
class ImportPath:
	root: Path
	module: str


class PythonProject(BaseProject):
	server_dependency: str

	@property
	def dependencies(self) -> list[str]:
		"""Package names from requirements.txt and pyproject.toml, lowercased with `-` as the separator."""
		names = []
		if (self.path / "requirements.txt").is_file():
			names += self.get_requirement_names(self.project_tree.readfile("requirements.txt").splitlines())

		if (self.path / "pyproject.toml").is_file():
			pyproject = tomllib.loads(self.project_tree.readfile("pyproject.toml"))
			names += self.get_requirement_names(pyproject.get("project", {}).get("dependencies", []))
			names += pyproject.get("tool", {}).get("poetry", {}).get("dependencies", {}).keys()

		return sorted({re.sub(r"[-_.]+", "-", name).lower() for name in names} - {"python"})

	def get_requirement_names(self, requirements: list[str]) -> list[str]:
		"""Names from PEP 508 requirement strings, skipping comments and pip options such as `-r`."""
		return [match.group(1) for line in requirements if (match := REQUIREMENT_NAME_PATTERN.match(line))]

	@property
	def python_version(self) -> str:
		""".python-version if it holds a plain CPython version, else the newest supported one pyproject allows."""
		if (self.path / ".python-version").is_file():
			version = self.project_tree.readfile(".python-version").strip()
			if PYTHON_VERSION_PATTERN.fullmatch(version):
				return version
		return next(iter(self.allowed_python_versions), DEFAULT_PYTHON_VERSION)

	@property
	def allowed_python_versions(self) -> list[str]:
		"""Supported versions inside `requires-python` or Poetry's `python` constraint, newest first."""
		if not (self.path / "pyproject.toml").is_file():
			return []
		pyproject = tomllib.loads(self.project_tree.readfile("pyproject.toml"))
		poetry_constraint = pyproject.get("tool", {}).get("poetry", {}).get("dependencies", {}).get("python")
		requirement = pyproject.get("project", {}).get("requires-python") or poetry_constraint
		if not isinstance(requirement, str):
			return []
		try:
			constraint = parse_constraint(requirement)
		except ValueError:
			return []
		return [version for version in SUPPORTED_PYTHON_VERSIONS if constraint.allows(Version.parse(version))]

	@property
	def package_manager(self) -> str:
		if (self.path / "uv.lock").is_file():
			return "uv"
		if (self.path / "poetry.lock").is_file():
			return "poetry"
		if (self.path / "requirements.txt").is_file():
			return "requirements"
		return "pyproject"

	@property
	def dockerfile(self) -> str:
		"""Dockerfile for the detected app. Secrets are not baked in; they reach the container as env at runtime."""
		return TEMPLATES.get_template("Dockerfile.jinja").render(
			python_version=self.python_version,
			package_manager=self.package_manager,
			server_dependency=self.server_dependency,
			port=self.default_run_port,
			command=json.dumps(shlex.split(self.start_command)),
		)

	def find_call(self, pattern: str) -> SemgrepFind | None:
		"""Shallowest semgrep match for `pattern` outside tests and docs; `filepath` is relative to the root."""
		result = subprocess.run(
			[
				"semgrep",
				"-e",
				pattern,
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
			SemgrepFind(
				Path(match["path"]).resolve().relative_to(self.project_tree.path), match["start"]["line"]
			)
			for match in json.loads(result.stdout).get("results", [])
		]
		matches = [match for match in matches if not IGNORED_DIRECTORIES.intersection(match.filepath.parts)]
		return min(
			matches, key=lambda match: (len(match.filepath.parts), match.filepath, match.lineno), default=None
		)

	def get_import_path(self, filepath: Path) -> ImportPath:
		"""Walk up from the file while each folder is a package, then return that folder and the module path."""
		imported_names, relative_level = self.get_imports(filepath)
		root = filepath.parent
		while root.parts and (
			(self.path / root / "__init__.py").is_file()
			or imported_names.intersection(root.parts)
			or len(filepath.parent.parts) - len(root.parts) < relative_level
		):
			root = root.parent
		parts = filepath.relative_to(root).with_suffix("").parts
		if parts[-1] == "__init__":
			parts = parts[:-1]
		return ImportPath(root, ".".join(parts))

	def get_imports(self, filepath: Path) -> tuple[set[str], int]:
		"""Top-level names the file imports, and the most dots used in a relative import."""
		try:
			tree = ast.parse(self.project_tree.readfile(filepath))
		except (SyntaxError, ValueError):
			return set(), 0
		names, relative_level = set(), 0
		for node in ast.walk(tree):
			if isinstance(node, ast.Import):
				names |= {alias.name.split(".")[0] for alias in node.names}
			elif isinstance(node, ast.ImportFrom) and node.level:
				relative_level = max(relative_level, node.level)
			elif isinstance(node, ast.ImportFrom):
				names.add(node.module.split(".")[0])
		return names, relative_level

	def get_app(self, find: SemgrepFind | None) -> AppFind:
		"""Module-level variable or top-level function that holds the call semgrep found."""
		if not find:
			raise EntrypointNotFound("No app found. Set run.command in frappe_cloud_deploy.toml.")
		tree = ast.parse(self.project_tree.readfile(find.filepath))
		for node in tree.body:
			if not node.lineno <= find.lineno <= node.end_lineno:
				continue
			if isinstance(node, ast.Assign | ast.AnnAssign) and isinstance(node.value, ast.Call):
				target = node.targets[0] if isinstance(node, ast.Assign) else node.target
				if isinstance(target, ast.Name):
					return AppFind(target.id, is_factory=False)
			if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
				return AppFind(node.name, is_factory=True)
		raise EntrypointNotFound(
			f"App in {find.filepath} is not assigned to a module-level variable or returned by a top-level function. "
			"Set run.command in frappe_cloud_deploy.toml."
		)
