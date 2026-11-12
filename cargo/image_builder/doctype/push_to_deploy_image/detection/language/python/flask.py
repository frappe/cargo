from functools import cached_property

from cargo.image_builder.doctype.push_to_deploy_image.detection.base import Framework
from cargo.image_builder.doctype.push_to_deploy_image.detection.language.python import (
	Entrypoint,
	find_entrypoint,
)


class Flask(Framework):
	name = "flask"
	dependency = "flask"

	def matches(self, dependencies: set[str]) -> bool:
		self.has_migrations = "flask-migrate" in dependencies
		return super().matches(dependencies)

	@cached_property
	def entrypoint(self) -> Entrypoint:
		return find_entrypoint(self.source, "Flask", "flask")

	@property
	def working_directory(self) -> str:
		return self.entrypoint.working_directory

	@property
	def target(self) -> str:
		call = "()" if self.entrypoint.is_factory else ""
		return f"{self.entrypoint.module}:{self.entrypoint.attribute}{call}"

	@property
	def start_command(self) -> str:
		return f"gunicorn '{self.target}' --bind 0.0.0.0:{self.port}"

	@property
	def release_command(self) -> str | None:
		if self.has_migrations:
			return f"flask --app '{self.target}' db upgrade"
		return None
