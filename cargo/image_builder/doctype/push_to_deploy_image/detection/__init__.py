import tomllib

from cargo.image_builder.doctype.push_to_deploy_image.detection.base import (
	DetectionError,
	Project,
	SourceTree,
)
from cargo.image_builder.doctype.push_to_deploy_image.detection.language.python import (
	get_dependencies,
	get_package_manager,
	get_python_version,
)
from cargo.image_builder.doctype.push_to_deploy_image.detection.language.python.django import Django
from cargo.image_builder.doctype.push_to_deploy_image.detection.language.python.fastapi import FastAPI
from cargo.image_builder.doctype.push_to_deploy_image.detection.language.python.flask import Flask

CONFIG_FILE = "frappe_cloud_deploy.toml"
FRAMEWORKS = [Django, FastAPI, Flask]


def get_project(path: str) -> Project:
	"""The project config wins. Detection runs only when the repository has no config file."""
	source = SourceTree(path)
	if source.has_file(CONFIG_FILE):
		return load_config(source)
	return detect(source)


def detect(source: SourceTree) -> Project:
	dependencies = get_dependencies(source)
	for framework_class in FRAMEWORKS:
		framework = framework_class(source)
		if framework.matches(dependencies):
			return Project(
				language="python",
				version=get_python_version(source),
				package_manager=get_package_manager(source),
				framework=framework.name,
				start_command=framework.start_command,
				release_command=framework.release_command,
				port=framework.port,
				working_directory=framework.working_directory,
			)
	raise DetectionError(f"No supported framework found. Add {CONFIG_FILE} to the repository.")


def load_config(source: SourceTree) -> Project:
	config = tomllib.loads(source.read_text(CONFIG_FILE))
	app, run = config.get("app", {}), config.get("run", {})
	required = {
		"app.language": app.get("language"),
		"app.version": app.get("version"),
		"app.framework": app.get("framework"),
		"run.command": run.get("command"),
		"run.port": run.get("port"),
	}
	if missing := [field for field, value in required.items() if value is None]:
		raise DetectionError(f"{CONFIG_FILE} is missing: {', '.join(missing)}.")
	return Project(
		language=app["language"],
		version=str(app["version"]),
		package_manager=get_package_manager(source),
		framework=app["framework"],
		start_command=run["command"],
		release_command=run.get("release_command"),
		port=run["port"],
		working_directory=run.get("working_directory", "."),
	)
