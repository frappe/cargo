from functools import cached_property

from cargo.image_builder.doctype.push_to_deploy_image.detection.base import Framework
from cargo.image_builder.doctype.push_to_deploy_image.detection.language.python import (
	Entrypoint,
	find_entrypoint,
)


class FastAPI(Framework):
	name = "fastapi"
	dependency = "fastapi"

	@cached_property
	def entrypoint(self) -> Entrypoint:
		return find_entrypoint(self.source, "FastAPI", "fastapi")

	@property
	def working_directory(self) -> str:
		return self.entrypoint.working_directory

	@property
	def start_command(self) -> str:
		factory = " --factory" if self.entrypoint.is_factory else ""
		return (
			f"uvicorn {self.entrypoint.module}:{self.entrypoint.attribute}{factory} "
			f"--host 0.0.0.0 --port {self.port}"
		)

	@property
	def release_command(self) -> str | None:
		if self.source.has_file("alembic.ini"):
			return "alembic upgrade head"
		return None
