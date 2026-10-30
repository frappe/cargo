### Cargo

App to manage and provision all frappe cloud services

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app cargo
```

### Pilot image capture

Cargo flushes the build machine's disk, requests an Atlas stop, and waits up to five minutes for Atlas to report `stopped` before taking the image. A flush error, stop error, or stop timeout fails the build without requesting an image. A retried capture can use an already-stopped builder without SSH. Atlas may force the stop if guest shutdown times out; the stopped state alone does not prove a clean database shutdown.

Base and Apps images request caching and warm preparation. Atlas boots the published disk image on each host and prepares its memory snapshot there. Bare Site images, with no signup app preinstalled, request neither caching nor memory snapshots. They remain available as disk images for building Apps images.

### Docs

- [Object storage](docs/object-storage.md)
- [Cluster health](docs/health.md)
- [Images](docs/image.md)
- [Bootstrapping](docs/bootstrapping.md)
- [What Cargo needs from Atlas](docs/atlas-contract.md)
- [What Cargo and Central say to each other](docs/central-contract.md)
- [Abuse logs and metrics](docs/abuse-logs.md) (planned): the log bucket and its keys, per-VM abuse metrics, and the `watcher` read user
- [Watcher](docs/watcher.md) (planned): abuse detection, feeds, log integrity, the `abuse` report to Central, and `attribute_address`

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/cargo
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade
### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.


### License

mit
