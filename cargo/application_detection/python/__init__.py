import re
import tomllib

from cargo.application_detection import BaseProject

REQUIREMENT_NAME_PATTERN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


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
