import re
from functools import cached_property
from pathlib import PurePosixPath

from cargo.image_builder.doctype.push_to_deploy_image.detection.base import DetectionError, Framework

SETTINGS_MODULE_PATTERN = re.compile(r"""DJANGO_SETTINGS_MODULE["']\s*,\s*["']([\w.]+)["']""")


class Django(Framework):
	name = "django"
	dependency = "django"
	release_command = "python manage.py migrate --noinput"

	def matches(self, dependencies: set[str]) -> bool:
		return super().matches(dependencies) and bool(self.source.find("manage.py"))

	@cached_property
	def manage_py(self) -> str:
		candidates = self.source.find("manage.py")
		if len(candidates) > 1:
			raise DetectionError(
				f"Found more than one manage.py: {', '.join(candidates)}. "
				"Set start_command in frappe_cloud_deploy.toml."
			)
		return candidates[0]

	@property
	def working_directory(self) -> str:
		return PurePosixPath(self.manage_py).parent.as_posix()

	@cached_property
	def settings_module(self) -> str:
		match = SETTINGS_MODULE_PATTERN.search(self.source.read_text(self.manage_py))
		if not match:
			raise DetectionError(f"{self.manage_py} does not set DJANGO_SETTINGS_MODULE.")
		return match.group(1)

	@cached_property
	def wsgi_module(self) -> str:
		package = self.settings_module.split(".")[0]
		wsgi_path = PurePosixPath(self.working_directory) / package / "wsgi.py"
		if not self.source.has_file(wsgi_path.as_posix()):
			raise DetectionError(f"Expected {wsgi_path} for settings module {self.settings_module}.")
		return f"{package}.wsgi"

	@property
	def start_command(self) -> str:
		return f"gunicorn {self.wsgi_module}:application --bind 0.0.0.0:{self.port}"
