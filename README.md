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

### Branches and releases

`develop` gets all new work. `master` is the stable branch that production runs, and it gets only backports from `develop`.

1. Raise the pull request on `develop`.
2. Add the `backport master` label when the change must go to production. After the merge, Mergify opens the backport pull request, assigns it to you, and merges it after the checks pass, in the order the source pull requests merged on `develop`.
3. Mergify closes other pull requests to `master`. For a manual backport, add the `manual-backport-pr` label and reopen the pull request.

A release manager runs the **Release** workflow on `master`. It tags the `master` head and creates a GitHub Release with notes from the merged pull requests. The version starts at `v1.0.0`. A breaking change, marked with `!` such as `feat(storage)!: Remove a field`, increases the minor version. Other changes increase the patch version.

### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every pull request and every push to `develop` and `master`.
- Release: Tags `master` and creates a GitHub Release. A release manager runs it from the Actions tab.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.


### License

mit

### Beacon rules

Beacon reads its alert rules and runbooks for Pilot image builds, object storage and Cargo errors from `beacon/` in this repository. Each minute, Cargo sends the health verdict of each live cluster as `cargo_object_storage_health`, with Unknown when the gateway cannot be read. Each hour, Cargo sends the age of the newest metadata backup of each live cluster as `cargo_object_storage_metadata_backup_age_seconds`. Each minute, it sends its new Error Logs, without form data, to the same Datum. `beacon/rules/<rule-key>.sql` is one alert rule and `beacon/runbooks/<rule-key>.md` tells the responder what to do. The `.http` and `.tls` files are checks that Beacon makes from outside each region: the health of `s3-admin-svc` and `telemetry-svc`, and the certificates of `s3-svc`, `telemetry-svc` and `telemetry-read-svc`. Change a rule in the same pull request as the metric that it reads. Beacon applies the change when staff click Sync Definitions in Beacon Settings.
