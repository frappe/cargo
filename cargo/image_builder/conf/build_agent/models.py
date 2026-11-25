import json
import re
from base64 import b64encode
from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

SECRETS_DIRECTORY = Path("/run/deploy-agent")
BUILD_UNIT_PREFIX = "deploy-build-"
RELEASE_UNIT_PREFIX = "release-build-"
# Central names a deploy request <app>-<6 characters of the commit>, and -1, -2 for a commit deployed again.
DEPLOY_REQUEST = re.compile(r"[0-9a-z]+(-[0-9a-z]+)*")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
COMMIT = re.compile(r"[0-9a-f]{40}")
HEALTH_PATH = re.compile(r"(/[A-Za-z0-9._~%/-]*)?")
SECRET_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# The slot file sets these for app@.service, so an app's secret must not replace them.
RESERVED_SECRET_NAMES = {"IMAGE", "PORT", "DEPLOY_REQUEST"}
DEFAULT_HEALTH_TIMEOUT_SECONDS = 60
MAX_HEALTH_TIMEOUT_SECONDS = 600


def get_environment_file(unit: str) -> Path:
	"""Root-only, named after its unit, so the agent finds it from the unit alone."""
	return SECRETS_DIRECTORY / f"{unit.removesuffix('.service')}.env"


def payload_file(deploy_request: str) -> Path:
	"""Central's build payload, kept root-only until the deploy request's release finishes."""
	return SECRETS_DIRECTORY / f"{deploy_request}.json"


def pick(cls: type, payload: dict) -> dict:
	"""The payload's fields that `cls` has; the rest belong to the other step."""
	return {item.name: payload[item.name] for item in fields(cls) if item.name in payload}


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
	secrets: dict[str, str] = field(default_factory=dict, repr=False)

	def __post_init__(self):
		if not DEPLOY_REQUEST.fullmatch(self.deploy_request):
			raise ValueError(f"Invalid deploy request {self.deploy_request!r}.")
		if not COMMIT.fullmatch(self.commit):
			raise ValueError(f"Invalid commit {self.commit!r}.")
		if not HEALTH_PATH.fullmatch(self.health_path):
			raise ValueError(f"Invalid health path {self.health_path!r}.")
		if not 0 < self.health_timeout <= MAX_HEALTH_TIMEOUT_SECONDS:
			raise ValueError(f"Invalid health timeout {self.health_timeout!r}.")
		for name, value in self.secrets.items():
			if not SECRET_NAME.fullmatch(name) or name in RESERVED_SECRET_NAMES:
				raise ValueError(f"Invalid secret name {name!r}.")
			# The slot file holds one secret per line.
			if "\n" in value or "\r" in value:
				raise ValueError(f"Secret {name} has a line break.")

	@classmethod
	def load(cls, deploy_request: str) -> "Release":
		return cls(**pick(cls, json.loads(payload_file(deploy_request).read_text())))

	@property
	def unit(self) -> str:
		return f"{RELEASE_UNIT_PREFIX}{self.deploy_request}.service"


@dataclass(frozen=True)
class Build:
	"""What build.sh needs from get_build's payload."""

	deploy_request: str
	repository: str
	commit: str
	token: str = field(repr=False)

	def __post_init__(self):
		if not REPOSITORY.fullmatch(self.repository):
			raise ValueError(f"Invalid repository {self.repository!r}.")
		if not DEPLOY_REQUEST.fullmatch(self.deploy_request):
			raise ValueError(f"Invalid deploy request {self.deploy_request!r}.")
		if not COMMIT.fullmatch(self.commit):
			raise ValueError(f"Invalid commit {self.commit!r}.")

	@classmethod
	def from_payload(cls, payload: dict) -> "Build":
		"""Checks the release's fields too, so a payload that cannot release fails before it builds."""
		Release(**pick(Release, payload))
		return cls(**pick(cls, payload))

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
			"COMMIT": self.commit,
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
