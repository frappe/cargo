from pathlib import Path

from cargo.application_detection import BaseProject
from cargo.application_detection.exceptions import NoSupportedFrameworksFound
from cargo.application_detection.python.django import Django
from cargo.application_detection.python.fastapi import FastAPI

# Will add more here as they come, fastapi, flask etc.
SUPPORTED_FRAMEWORKS = {"python": [Django, FastAPI]}
DECLARED_BUILD_FILES = ("dockerfile", "frappe_cloud_deploy.toml")


def has_declared_build_system(path: Path) -> tuple[str, str] | None:
	"""Returns dockerfile or frappe cloud deploy file if present prefers dockerfile"""
	files = {item.name.casefold(): item for item in path.iterdir() if item.is_file()}
	for name in DECLARED_BUILD_FILES:
		if name in files:
			return name, files[name].read_text()
	return None


def get_project_language(path: Path) -> str:
	"""Implement this later"""
	return "python"


def detect_project(path: str | Path) -> BaseProject | None:
	"""Will add a language filter later, for now pass the path to the only language frameworks
	we support as of now and the first one that matches is our project.

	Rules:
	- A dockerfile already exists?
	- A frappe_cloud_deploy.toml already exists?
	- Run detection if not
	"""

	path = Path(path) if isinstance(path, str) else path
	declared_build_system = has_declared_build_system(path)
	if declared_build_system:
		# Implement what needs to be done here.
		...
		return None

	project_language = get_project_language(path)
	supported_language_frameworks = SUPPORTED_FRAMEWORKS.get(project_language)
	if not supported_language_frameworks:
		raise NoSupportedFrameworksFound(f"Could not find supported frameworks for {project_language}")

	for framework_class in supported_language_frameworks:
		framework = framework_class(path)
		if framework.matches():
			return framework

	raise NoSupportedFrameworksFound(
		f"No supported {project_language} framework found. Add a Dockerfile or frappe_cloud_deploy.toml."
	)
