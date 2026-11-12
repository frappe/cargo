import ast
import re
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath

from cargo.image_builder.doctype.push_to_deploy_image.detection.base import DetectionError, SourceTree

DEFAULT_PYTHON_VERSION = "3.12"
REQUIREMENT_NAME_PATTERN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
ENTRYPOINT_FILES = (
	"wsgi.py",
	"asgi.py",
	"app.py",
	"application.py",
	"main.py",
	"server.py",
	"api.py",
	"run.py",
	"__init__.py",
)
IGNORED_DIRECTORIES = {"tests", "test", "docs", "examples", "migrations"}


@dataclass
class Entrypoint:
	module: str
	attribute: str
	working_directory: str
	is_factory: bool = False


def find_entrypoint(source: SourceTree, class_name: str, package: str) -> Entrypoint:
	"""First module-level `<package>.<class_name>()` app or factory, searching likely files from the shallowest up."""
	for filename in ENTRYPOINT_FILES:
		for path in sorted(source.find(filename), key=lambda path: path.count("/")):
			if path == "__init__.py" or IGNORED_DIRECTORIES.intersection(PurePosixPath(path).parts):
				continue
			if app := find_app_in_module(source.read_text(path), class_name, package):
				attribute, is_factory = app
				return get_entrypoint(path, attribute, is_factory)
	raise DetectionError(f"No {class_name}() app found. Set run.command in frappe_cloud_deploy.toml.")


def find_app_in_module(text: str, class_name: str, package: str) -> tuple[str, bool] | None:
	"""(attribute, is_factory) for a module-level instance, else a top-level function that builds one."""
	try:
		tree = ast.parse(text)
	except (SyntaxError, ValueError):
		return None
	names = get_imported_names(tree, class_name, package)
	if not names:
		return None
	for node in tree.body:
		if isinstance(node, ast.Assign | ast.AnnAssign) and is_call_to(node.value, names):
			target = node.targets[0] if isinstance(node, ast.Assign) else node.target
			if isinstance(target, ast.Name):
				return target.id, False
	for node in tree.body:
		if isinstance(node, ast.FunctionDef) and any(is_call_to(child, names) for child in ast.walk(node)):
			return node.name, True
	return None


def get_imported_names(tree: ast.Module, class_name: str, package: str) -> set[str]:
	"""Names the module uses for `<package>.<class_name>`, following `as` aliases."""
	names = set()
	for node in tree.body:
		if isinstance(node, ast.ImportFrom) and node.module == package:
			names |= {alias.asname or alias.name for alias in node.names if alias.name == class_name}
		elif isinstance(node, ast.Import):
			names |= {
				f"{alias.asname or alias.name}.{class_name}" for alias in node.names if alias.name == package
			}
	return names


def is_call_to(node: ast.AST | None, names: set[str]) -> bool:
	return isinstance(node, ast.Call) and ast.unparse(node.func) in names


def get_entrypoint(path: str, attribute: str, is_factory: bool = False) -> Entrypoint:
	parts = PurePosixPath(path).with_suffix("").parts
	working_directory = "."
	if parts[0] == "src":
		working_directory, parts = "src", parts[1:]
	if parts[-1] == "__init__":
		parts = parts[:-1]
	return Entrypoint(".".join(parts), attribute, working_directory, is_factory)


def get_dependencies(source: SourceTree) -> set[str]:
	"""Package names from requirements.txt and pyproject.toml, normalised to lowercase with dashes."""
	names = []
	if source.has_file("requirements.txt"):
		names += [
			match.group(1)
			for line in source.read_text("requirements.txt").splitlines()
			if (match := REQUIREMENT_NAME_PATTERN.match(line))
		]
	if source.has_file("pyproject.toml"):
		pyproject = tomllib.loads(source.read_text("pyproject.toml"))
		names += [
			match.group(1)
			for requirement in pyproject.get("project", {}).get("dependencies", [])
			if (match := REQUIREMENT_NAME_PATTERN.match(requirement))
		]
		names += pyproject.get("tool", {}).get("poetry", {}).get("dependencies", {}).keys()
	return {re.sub(r"[-_.]+", "-", name).lower() for name in names}


def get_python_version(source: SourceTree) -> str:
	if source.has_file(".python-version"):
		return source.read_text(".python-version").strip()
	return DEFAULT_PYTHON_VERSION


def get_package_manager(source: SourceTree) -> str:
	if source.has_file("uv.lock"):
		return "uv"
	if source.has_file("poetry.lock"):
		return "poetry"
	if source.has_file("requirements.txt") or source.has_file("pyproject.toml"):
		return "pip"
	raise DetectionError("No requirements.txt or pyproject.toml found.")
