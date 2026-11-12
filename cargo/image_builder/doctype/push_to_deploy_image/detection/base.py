import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


class DetectionError(Exception):
	pass


@dataclass
class Project:
	language: str
	version: str
	package_manager: str
	framework: str
	start_command: str
	release_command: str | None = None
	port: int = 8000
	working_directory: str = "."


class SourceTree:
	"""Read-only view of a cloned repository. Never reads outside the repository root."""

	SKIPPED_DIRECTORIES: ClassVar[set[str]] = {".git", "node_modules", ".venv", "venv", "__pycache__"}

	def __init__(self, root: str | Path):
		self.root = Path(root).resolve()

	def has_file(self, relative_path: str) -> bool:
		return self.resolve(relative_path).is_file()

	def read_text(self, relative_path: str) -> str:
		return self.resolve(relative_path).read_text(errors="replace")

	def find(self, name: str, max_depth: int = 2) -> list[str]:
		matches = []
		for directory, subdirectories, files in os.walk(self.root):
			depth = len(Path(directory).relative_to(self.root).parts)
			subdirectories[:] = [
				subdirectory
				for subdirectory in subdirectories
				if subdirectory not in self.SKIPPED_DIRECTORIES and depth < max_depth
			]
			if name in files:
				matches.append((Path(directory) / name).relative_to(self.root).as_posix())
		return sorted(matches)

	def resolve(self, relative_path: str) -> Path:
		path = (self.root / relative_path).resolve()
		if not path.is_relative_to(self.root):
			raise DetectionError(f"{relative_path} points outside the repository.")
		return path


class Framework:
	"""Decides how a project runs. The language decides how it builds."""

	name: str
	dependency: str
	port = 8000
	release_command: str | None = None

	def __init__(self, source: SourceTree):
		self.source = source

	def matches(self, dependencies: set[str]) -> bool:
		return self.dependency in dependencies

	@property
	def working_directory(self) -> str:
		return "."

	@property
	def start_command(self) -> str:
		raise NotImplementedError
