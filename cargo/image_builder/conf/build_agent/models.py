import re
from base64 import b64encode
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

SECRETS_DIRECTORY = Path("/run/deploy-agent")
UNIT_PREFIX = "deploy-build-"
DEPLOY_REQUEST = re.compile(r"[0-9a-z]+")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
COMMIT = re.compile(r"[0-9a-f]{40}")


class BuildStatus(StrEnum):
	"""Matches BUILD_REPORT_STATES in central.deploy.api."""

	RUNNING = "Running"
	SUCCEEDED = "Succeeded"
	FAILED = "Failed"


@dataclass(frozen=True)
class Build:
	"""What get_build hands the agent."""

	deploy_request: str
	repository: str
	commit: str
	token: str = field(repr=False)

	def __post_init__(self):
		if not DEPLOY_REQUEST.fullmatch(self.deploy_request):
			raise ValueError(f"Invalid deploy request {self.deploy_request!r}.")
		if not REPOSITORY.fullmatch(self.repository):
			raise ValueError(f"Invalid repository {self.repository!r}.")
		if not COMMIT.fullmatch(self.commit):
			raise ValueError(f"Invalid commit {self.commit!r}.")

	@property
	def unit(self) -> str:
		return f"{UNIT_PREFIX}{self.deploy_request}.service"

	@property
	def environment_file(self) -> Path:
		return SECRETS_DIRECTORY / f"{self.deploy_request}.env"

	@property
	def environment(self) -> dict[str, str]:
		"""The token goes in an HTTP header, so it is never written to the clone's config or the log."""
		basic = b64encode(f"x-access-token:{self.token}".encode()).decode()
		return {
			"REPOSITORY": self.repository,
			"COMMIT": self.commit,
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
class BuildUnit:
	"""The systemd unit running a build, named after its Deploy Request."""

	name: str
	sub_state: str
	result: str

	@property
	def deploy_request(self) -> str:
		return self.name.removeprefix(UNIT_PREFIX).removesuffix(".service")

	@property
	def environment_file(self) -> Path:
		return SECRETS_DIRECTORY / f"{self.deploy_request}.env"

	@property
	def is_finished(self) -> bool:
		return self.sub_state in {"exited", "failed", "dead"}

	@property
	def is_out_of_memory(self) -> bool:
		return self.result == "oom-kill"

	@property
	def status(self) -> BuildStatus:
		if not self.is_finished:
			return BuildStatus.RUNNING
		# `dead` with `success` is a unit someone stopped by hand, not a finished build.
		if self.sub_state == "exited" and self.result == "success":
			return BuildStatus.SUCCEEDED
		return BuildStatus.FAILED
