import re
from base64 import b64encode
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

SECRETS_DIRECTORY = Path("/run/deploy-agent")
BUILD_UNIT_PREFIX = "deploy-build-"
RELEASE_UNIT_PREFIX = "release-build-"
DEPLOY_REQUEST = re.compile(r"[0-9a-z]+")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
COMMIT = re.compile(r"[0-9a-f]{40}")
HEALTH_PATH = re.compile(r"(/[A-Za-z0-9._~%/-]*)?")
DEFAULT_HEALTH_TIMEOUT_SECONDS = 60
MAX_HEALTH_TIMEOUT_SECONDS = 600


def get_environment_file(unit: str) -> Path:
	"""Root-only, named after its unit, so the agent finds it from the unit alone."""
	return SECRETS_DIRECTORY / f"{unit.removesuffix('.service')}.env"


class BuildStatus(StrEnum):
	"""Matches BUILD_REPORT_STATES in central.deploy.api."""

	RUNNING = "Running"
	SUCCEEDED = "Succeeded"
	FAILED = "Failed"


class ReleaseStatus(StrEnum):
	"""What report_deployment_status in central.deploy.api accepts."""

	RELEASING = "Releasing"
	LIVE = "Live"
	FAILED = "Failed"


@dataclass(frozen=True)
class Release:
	"""What release.py needs; no token, since it only runs the built image."""

	deploy_request: str
	commit: str
	health_path: str = ""
	health_timeout: int = DEFAULT_HEALTH_TIMEOUT_SECONDS

	def __post_init__(self):
		if not DEPLOY_REQUEST.fullmatch(self.deploy_request):
			raise ValueError(f"Invalid deploy request {self.deploy_request!r}.")
		if not COMMIT.fullmatch(self.commit):
			raise ValueError(f"Invalid commit {self.commit!r}.")
		if not HEALTH_PATH.fullmatch(self.health_path):
			raise ValueError(f"Invalid health path {self.health_path!r}.")
		if not 0 < self.health_timeout <= MAX_HEALTH_TIMEOUT_SECONDS:
			raise ValueError(f"Invalid health timeout {self.health_timeout!r}.")

	@classmethod
	def from_environment(cls, environment: dict[str, str]) -> "Release":
		"""The release a build's environment describes."""
		return cls(
			environment["DEPLOY_REQUEST"],
			environment["COMMIT"],
			environment["HEALTH_PATH"],
			int(environment["HEALTH_TIMEOUT"]),
		)

	@property
	def unit(self) -> str:
		return f"{RELEASE_UNIT_PREFIX}{self.deploy_request}.service"

	@property
	def environment_file(self) -> Path:
		return get_environment_file(self.unit)

	@property
	def environment(self) -> dict[str, str]:
		return {
			"DEPLOY_REQUEST": self.deploy_request,
			"COMMIT": self.commit,
			"HEALTH_PATH": self.health_path,
			"HEALTH_TIMEOUT": str(self.health_timeout),
		}


@dataclass(frozen=True)
class Build:
	"""What get_build hands the agent."""

	deploy_request: str
	repository: str
	commit: str
	token: str = field(repr=False)
	health_path: str = ""
	health_timeout: int = DEFAULT_HEALTH_TIMEOUT_SECONDS

	def __post_init__(self):
		if not REPOSITORY.fullmatch(self.repository):
			raise ValueError(f"Invalid repository {self.repository!r}.")
		# Release checks the fields the two share.
		self.release

	@property
	def release(self) -> Release:
		return Release(self.deploy_request, self.commit, self.health_path, self.health_timeout)

	@property
	def unit(self) -> str:
		return f"{BUILD_UNIT_PREFIX}{self.deploy_request}.service"

	@property
	def environment_file(self) -> Path:
		return get_environment_file(self.unit)

	@property
	def environment(self) -> dict[str, str]:
		"""The token goes in an HTTP header, so it is never written to the clone's config or the log."""
		basic = b64encode(f"x-access-token:{self.token}".encode()).decode()
		return {
			**self.release.environment,
			"REPOSITORY": self.repository,
			"GIT_CONFIG_COUNT": "1",
			"GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
			"GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
		}


@dataclass(frozen=True)
class BuildReport:
	"""What the agent sends report_build."""

	deploy_request: str
	status: BuildStatus
	log: str


@dataclass(frozen=True)
class ReleaseReport:
	"""What the agent sends report_deployment_status."""

	deploy_request: str
	status: ReleaseStatus
	logs: str


@dataclass(frozen=True)
class Unit:
	"""A systemd unit the agent started, named after its Deploy Request."""

	PREFIX: ClassVar[str]

	name: str
	sub_state: str
	result: str

	@property
	def deploy_request(self) -> str:
		return self.name.removeprefix(self.PREFIX).removesuffix(".service")

	@property
	def environment_file(self) -> Path:
		return get_environment_file(self.name)

	@property
	def cursor_file(self) -> Path:
		"""Where the journal lines Central already has end."""
		return SECRETS_DIRECTORY / f"{self.name.removesuffix('.service')}.cursor"

	@property
	def is_finished(self) -> bool:
		return self.sub_state in {"exited", "failed", "dead"}

	@property
	def is_out_of_memory(self) -> bool:
		return self.result == "oom-kill"

	@property
	def has_succeeded(self) -> bool:
		"""`dead` with `success` is a unit someone stopped by hand, not one that finished."""
		return self.sub_state == "exited" and self.result == "success"


class BuildUnit(Unit):
	PREFIX = BUILD_UNIT_PREFIX

	@property
	def status(self) -> BuildStatus:
		if not self.is_finished:
			return BuildStatus.RUNNING
		return BuildStatus.SUCCEEDED if self.has_succeeded else BuildStatus.FAILED


class ReleaseUnit(Unit):
	PREFIX = RELEASE_UNIT_PREFIX

	@property
	def status(self) -> ReleaseStatus:
		if not self.is_finished:
			return ReleaseStatus.RELEASING
		return ReleaseStatus.LIVE if self.has_succeeded else ReleaseStatus.FAILED
