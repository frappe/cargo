import ast
import shlex
from functools import cached_property
from pathlib import Path

from cargo.application_detection.exceptions import EntrypointNotFound
from cargo.application_detection.python import PythonProject


class Django(PythonProject):
	dependency_name = "django"
	default_run_port = 8000

	def matches(self) -> bool:
		"""Has manage.py, a settings.py that declares SECRET_KEY and a django dependency."""
		return bool(self.manage_file and self.settings_file and self.dependency_name in self.dependencies)

	@cached_property
	def manage_file(self) -> Path | None:
		return self.project_tree.find("manage.py")

	@cached_property
	def settings_file(self) -> Path | None:
		return self.project_tree.find("settings.py")

	@property
	def root_path(self) -> str:
		"""Directory holding manage.py, relative to the project root."""
		return str(self.manage_file.parent)

	@property
	def entrypoint(self) -> str:
		"""WSGI app from the WSGI_APPLICATION setting, such as `shop.wsgi:application`."""
		if not self.settings_file:
			raise EntrypointNotFound("No settings.py declaring SECRET_KEY found")

		setting = self.get_setting(self.settings_file, "WSGI_APPLICATION")
		wsgi_application = setting.value if isinstance(setting, ast.Constant) else None
		parts = wsgi_application.split(".") if isinstance(wsgi_application, str) else []
		if len(parts) < 2 or not all(part.isidentifier() for part in parts):
			raise EntrypointNotFound(
				f"No WSGI_APPLICATION found in {self.settings_file}. Set run.command in frappe_cloud_deploy.toml."
			)
		module, attribute = wsgi_application.rsplit(".", 1)
		return f"{module}:{attribute}"

	@property
	def pre_release_command(self):
		return "python manage.py migrate"

	@property
	def start_command(self) -> str:
		return f"gunicorn --chdir {shlex.quote(self.root_path)} {self.entrypoint} --bind 0.0.0.0:{self.default_run_port}"

	def get_setting(self, settings_file: Path, name: str) -> ast.expr | None:
		"""Value assigned to `name` at the top level of a settings file, without running it."""
		try:
			tree = ast.parse(self.project_tree.readfile(settings_file))
		except (SyntaxError, ValueError):
			return None
		for node in tree.body:
			if isinstance(node, ast.Assign):
				targets = node.targets
			elif isinstance(node, ast.AnnAssign):
				targets = [node.target]
			else:
				continue
			if any(isinstance(target, ast.Name) and target.id == name for target in targets):
				return node.value
		return None
