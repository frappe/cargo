# Set up a Cargo host

## Purpose

Cargo runs on one virtual machine in each Atlas region. Atlas creates the virtual machine, installs Cargo, and publishes the Cargo route through the regional Proxy.

```text
Atlas SSH ----------------------> Cargo public IPv4
cargo.<wildcard-domain> -> Proxy -> Cargo mesh IPv6
cargo-pilot.<wildcard-domain> -> Proxy -> Cargo mesh IPv6
```

The public IPv4 address is for SSH and operations. The Proxy route must use the WireGuard mesh IPv6 address.

## Requirements

Atlas must have an Active Proxy Server, an Available Virtual Machine Image, and an unattached Allocated Metal Server IP Address. Atlas Settings must contain the region values, wildcard domain, public SSH key, Proxy cluster password, and JSON Web Token signing key.

## Atlas provisioning

Open the Cargo Server Single DocType and select Provision. Select the image and public IPv4 address. Confirm the default size or enter another size.

Atlas creates a privileged tenant `0` virtual machine with `uplink` egress. Atlas waits for SSH on the public IPv4 address and runs `atlas/scripts/install-cargo.sh` with a synchronous SSH Task.

Atlas generates the site password, Pilot admin password, Atlas token, and Proxy token immediately before installation. The SSH Task stores the environment and command output for operator visibility.

After installation, Atlas maps the `cargo` and `cargo-pilot` sites to the virtual machine mesh IPv6 address. Atlas then checks `https://cargo.<wildcard-domain>/api/method/ping` through the Proxy. The Cargo Server becomes Active only after this request returns `pong`.

## Manual installation

Use `setup.sh` when you must test or install Cargo without the Atlas provisioning action.

```bash
PILOT_ADMIN_PASSWORD=... \
SITE_PASSWORD=... \
ADMIN_DOMAIN=cargo-pilot.example.com \
SITE=cargo.example.com \
CENTRAL_URL=https://central.invalid \
JWKS_URL=https://atlas.example.com/api/atlas/jwks.json \
ATLAS_URL=https://atlas.example.com \
ATLAS_TOKEN=... \
ATLAS_TENANT_ID=0 \
PROXY_URL=https://proxy.example.com \
PROXY_TOKEN=... \
WILDCARD_DOMAIN=example.com \
CARGO_URL=https://cargo.example.com \
CENTRAL_WEBHOOK_SECRET=not-configured \
REGION=blr \
REGION_ID=3 \
./setup.sh
```

`setup.sh` refuses to start when a required value is empty. Both passwords must contain at least eight characters, an uppercase letter, a lowercase letter, a number, and a symbol.

The installation contract has these values:

| Variable | Purpose |
|---|---|
| `PILOT_ADMIN_PASSWORD` | Password for the Pilot administration site. |
| `SITE_PASSWORD` | Password for the Cargo Frappe Administrator. |
| `SITE` | Cargo site name and public domain. |
| `ADMIN_DOMAIN` | Pilot administration domain. Atlas uses `cargo-pilot.<wildcard-domain>`. |
| `ATLAS_URL` | Atlas base URL. |
| `ATLAS_TOKEN` | Encrypted bearer token for the Atlas tenant API. |
| `ATLAS_TENANT_ID` | Atlas tenant. The regional Cargo service uses `0`. |
| `PROXY_URL` | Regional Proxy control API URL. |
| `PROXY_TOKEN` | Encrypted token restricted to site names with the `-svc` suffix. |
| `WILDCARD_DOMAIN` | Regional wildcard domain used for public service routes. |
| `JWKS_URL` | Atlas merged JSON Web Key Set route. |
| `CARGO_URL` | Public Cargo URL through the Proxy. |
| `REGION` and `REGION_ID` | Atlas region name and numeric region ID. |
| `CENTRAL_URL` | Central URL. Atlas uses `https://central.invalid` until this interface is available. |
| `CENTRAL_WEBHOOK_SECRET` | Central webhook secret. Atlas uses `not-configured` until this interface is available. |

## Installation operation

The script creates a `frappe` system user, installs Pilot, creates the bench and site, installs Cargo, deploys the workload, and restarts its workers. The Cargo install hook writes the configuration to Cargo Settings and completes the Frappe setup wizard.

Cargo Settings stores `atlas_token`, `proxy_token`, and `central_webhook_secret` as encrypted Password fields. Cargo reads these values with `get_password` when it makes an authenticated request.

Set `CI` to make the install hook skip configuration. Use this only when CI installs the app without service endpoints.

## Domains

The Cargo site and Pilot administration site listen on plain HTTP port 80 inside the virtual machine. The regional Proxy terminates public TLS and forwards traffic to the mesh IPv6 address.

The existing regional wildcard DNS record covers `cargo.<wildcard-domain>` and `cargo-pilot.<wildcard-domain>`. Do not create direct A records for these domains.

## Object storage routes

Cargo maps `s3-svc.<wildcard-domain>` and `s3-admin-svc.<wildcard-domain>` to the active object storage gateway mesh address before it marks the cluster Active. A Proxy failure marks the cluster Failed so that setup can retry.

Cargo permits only one Active Object Storage Cluster. Other cluster records can remain for archival history, but setup stops while another cluster is Active.

A host builds its own cluster when the site config holds `default_storage_cluster_config`. Without that key, an operator builds it from the desk. Read [object storage](object-storage.md).

## Authentication

Cargo calls Atlas with `Authorization: Bearer <atlas_token>` and `X-Tenant-ID: 0`. The token has audience `atlas-admin:<region-id>`, subject `cargo`, scope `*`, tenant `0`, and a 365-day lifetime.

The Proxy token has audience `atlas-proxy:<region-id>`, subject `cargo`, scope `site:*`, a `constraints.site.suffix` value of `-svc`, no tenant claim, and a 365-day lifetime. Cargo also refuses to call the Proxy for a site name without this suffix.

Inbound Cargo API tokens use the `X-Cargo-Access-Token` header. Cargo verifies them with the merged Atlas key set and requires the audience `atlas-cargo:<region-id>`. Cargo issuer validation is deferred.

## Validation

Run `tools/e2e/run.sh` to install the current Cargo worktree in a temporary Ubuntu container. The check confirms the bench, Cargo site, Cargo Settings, systemd units, and nginx response.
