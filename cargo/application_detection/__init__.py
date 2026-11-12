"""This is the base for application detection in Cargo."""

import os
from abc import ABC, abstractmethod
from pathlib import Path

from cargo.application_detection.exceptions import PathOutsideProject, ProjectDoesNotExist

SKIPPED_DIRECTORIES = {".git", ".venv", "venv", "node_modules", "__pycache__"}


class ProjectTree:
	def __init__(self, path: Path):
		self.path = path.resolve()

	def find(self, name: str, max_depth: int = 3) -> Path | None:
		"""Shallowest file called `name`, relative to the project root."""
		matches = []
		for directory, subdirectories, files in os.walk(self.path):
			relative_directory = Path(directory).relative_to(self.path)
			subdirectories[:] = [
				subdirectory
				for subdirectory in subdirectories
				if subdirectory not in SKIPPED_DIRECTORIES and len(relative_directory.parts) < max_depth
			]
			if name in files:
				matches.append(relative_directory / name)
		return min(matches, key=lambda match: len(match.parts), default=None)

	def readfile(self, path: str | Path) -> str:
		"""Read a file in the project, refusing symlinks that point outside it."""
		file = (self.path / path).resolve()
		if not file.is_relative_to(self.path):
			raise PathOutsideProject(f"{path} points outside the project")
		return file.read_text()


class BaseProject(ABC):
	def __init__(self, path: str | Path):
		self.path = path if isinstance(path, Path) else Path(path)
		self.validate_project_path()
		self.project_tree = ProjectTree(self.path)

	def validate_project_path(self):
		"""Ensure project path is present and does not try to go outside itself."""
		if not self.path.exists() or not self.path.is_dir():
			raise ProjectDoesNotExist(f"No project found at {self.path}")

	@property
	def dependencies(self) -> list[str]:
		"""MAYBE JUST READ PYTHON DEPENDENCIES HERE?"""
		raise NotImplementedError

	@abstractmethod
	def matches(self) -> bool:
		raise NotImplementedError

	@property
	@abstractmethod
	def root_path(self) -> str:
		raise NotImplementedError

	@property
	@abstractmethod
	def entrypoint(self) -> str:
		raise NotImplementedError

	@property
	@abstractmethod
	def pre_release_command(self) -> str | None:
		raise NotImplementedError

	@property
	@abstractmethod
	def start_command(self) -> str:
		raise NotImplementedError
