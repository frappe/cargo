import ast
import json
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from cargo.application_detection import BaseProject
from cargo.application_detection.exceptions import EntrypointNotFound

REQUIREMENT_NAME_PATTERN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
IGNORED_DIRECTORIES = {"tests", "test", "docs", "examples", "scripts"}


@dataclass
class SemgrepFind:
	filepath: Path
	lineno: int


@dataclass
class AppFind:
	app_name: str  # Variable that stores the class initialization
	is_factory: bool  # Initiated via a function


class PythonProject(BaseProject):
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
