# Setting up a Cargo host

Cargo runs on its own machine, one per region. This is how a bare VM becomes a production
Cargo host that Central trusts.

The provisioner does the handing over. Everything the host needs to know is passed to it at
install time, in the environment, and written straight onto **Cargo Settings**. The host
asks nobody for its configuration afterwards.

## Before you start

Central needs an **Atlas Instance** for the region already, because the host will be calling
Atlas for machines and you have to give it Atlas's URL.

## Step 1 — create the Cargo Instance in Central

Create a **Cargo Instance** and set its **Region**. One Cargo per region. That is the only
field you fill in.

The region also needs an **Atlas Region ID** on its **Region** record — Atlas's own numeric
id, copied from that region's Atlas Settings. Central puts it in the audience of the token
Cargo presents, so a token minted for one region is refused in another.

## Step 2 — run the script on the new machine

```bash
PILOT_ADMIN_PASSWORD=... \
SITE_PASSWORD=... \
ADMIN_DOMAIN=pilot.blr.example.com \
SITE=cargo.blr.example.com \
CENTRAL_URL=https://central.example.com \
ATLAS_URL=https://atlas.example.com \
CARGO_URL=https://cargo-blr.example.com \
CENTRAL_WEBHOOK_SECRET=... \
REGION=blr \
REGION_ID=3 \
ATLAS_KEY=... \
ATLAS_SECRET=... \
ATLAS_TENANT_ID=7 \
./setup.sh
```

`setup.sh` refuses to start unless every one of them is set, and names the ones that are
missing. There is one variable per mandatory field of Cargo Settings and nothing else: the
install hook writes them straight onto it, so a missing one fails the install rather than
leaving a host that is half configured. `SITE` is the only one with a usable default, and it
is not one you want in production — see [Domains](#domains).

The two passwords are different things, and neither is the database password:

| | What it is |
|---|---|
| `PILOT_ADMIN_PASSWORD` | Logs in to pilot's own admin panel on this machine |
| `SITE_PASSWORD` | The Frappe `Administrator` password for the Cargo site |
| MariaDB root | You don't set it. Pilot generates one when it creates the bench. |
| `CENTRAL_WEBHOOK_SECRET` | Signs the reports this host sends Central. See [Reporting to Central](#reporting-to-central). |
| `REGION` / `REGION_ID` | The region's name and Atlas's numeric id for it |
| `ATLAS_KEY` / `ATLAS_SECRET` / `ATLAS_TENANT_ID` | This host's Atlas credentials, and the tenant every Atlas call is scoped to |

`REGION` must name the same **Region** you picked in step 1, and `REGION_ID` must match that
region's Atlas Region ID. Nothing checks either during install. A mismatched `REGION` fails
every later Central call with *"This Cargo token is not for region X"*; a mismatched
`REGION_ID` fails every Atlas call with a 403. See [One host, one region](#one-host-one-region).

The script then:

1. Runs pilot's installer, which brings Python, Node, MariaDB, Redis and nginx. The machine
   can be completely bare. Pilot is pinned to a release (`v0.0.29-pre-alpha`) rather than
   `develop`, so two hosts built weeks apart get the same pilot.
2. Creates a bench with `ADMIN_DOMAIN` as its admin domain, and a site named `SITE`.
3. Downloads the Cargo app.
4. Exports the nine variables above, then installs Cargo on the site.
5. Deploys the bench to production: systemd units for the workload, nginx in front of them.

`PILOT_VERSION`, `BENCH`, `BRANCH` and `REPO` can be overridden. `BRANCH` is Cargo's own
branch and still defaults to `develop`.

### Domains

Production serves two things, on two domains:

| | What it is |
|---|---|
| `ADMIN_DOMAIN` | pilot's admin panel for this machine, the one `PILOT_ADMIN_PASSWORD` logs in to |
| `SITE` | the Cargo site itself, the host that answers at `CARGO_URL` |

Both are served over plain HTTP on **port 80**. The script passes no `--tls`, so pilot does
not request certificates and nginx renders no HTTPS server block: HTTPS is expected to
terminate on the proxy in front of this host.

`SITE` defaults to `cargo.localhost`, which is fine for a throwaway box and wrong everywhere
else — the site name is the domain nginx serves, and `CARGO_URL` has to reach it. Set it to a
real hostname before running.

## Step 3 — what the install hook does

`cargo/install.py` runs on `after_install` and writes all nine values onto **Cargo Settings**.
That is the whole of it: no call goes out, and nothing has to be reachable for the install to
finish.

### Installing without configuring

With `CI` set in the environment, the hook returns immediately. That is CI, where the app is
installed with nothing to point it at.

Otherwise a missing variable fails the install, naming the ones it did not get. A
half-supplied set is a typo, not an intention.

## Reporting to Central

Cargo tells Central when a cluster becomes usable or fails. It does this with a **Webhook**,
made against the cluster when the cluster is created and firing on `Active` and `Failed`.

Frappe signs the body with `CENTRAL_WEBHOOK_SECRET` and sends the signature as
`X-Frappe-Webhook-Signature` — an HMAC-SHA256 of the JSON body, base64 encoded. The secret
itself never leaves the host, and the signature covers the payload, so Central can tell a
tampered report from a genuine one.

Central verifies it against the same secret, which the provisioner gave to both sides.

## Talking to Atlas

Every Atlas call carries two headers: `X-Atlas-Central-Token`, a JWT signed by Central, and
`X-Tenant-ID`, the tenant this host provisions into. Atlas verifies the token against
Central's published keys and checks its audience is `atlas-<region id>-admin` — which is why
`REGION_ID` has to be right.

## Central calling Cargo

Bucket work runs the other way: Central asks Cargo to make, rotate and drop buckets, because
Cargo owns the cluster's admin token and Central never sees it.

Those calls carry `X-Cargo-Access-Token`, a JWT that Cargo verifies against Central's JWKS at
`/api/method/central.api.jwks.get_jwks`. Cargo holds no secret for this — it needs only
Central's public keys, which it fetches from `CENTRAL_URL`. A token is accepted when its
audience is `central-admin` or this region's `atlas-<region id>-admin`.

This is the one path that needs the host to be reachable from Central, at `CARGO_URL`.

Tokens ride their own headers rather than `Authorization`: Frappe treats `Authorization` as
OAuth or an API key and rejects anything else with a 401 before the request reaches the
endpoint.

## One host, one region

A Cargo host may only act on the region its own Cargo Instance names. Every call that takes a
`region` is checked against it, in both directions — Central refuses a Cargo host asking
about another region, and Cargo refuses a caller naming a region that is not its own.

Without that check a valid token is a valid token: any Cargo host could name any region and
Central would answer for it — repointing another cluster's endpoints at a machine of the
caller's choosing, or marking it down. The token proves *a* Cargo host is calling; the
instance claim is what proves *which* one.

A host whose instance is set to **Disabled** is refused the same way, on every call. That is
how you take a host out of service without deleting anything.

## What Central knows and doesn't

Central stores each Cargo's region and base URL, and the region's storage endpoints once a
cluster reports itself up. Cargo keeps the cluster's own secrets — the Garage `rpc_secret`,
admin token and metrics token are minted on the host and never sent.

So if a Cargo host is down, provisioning new clusters and issuing new buckets stops, but
everything already running is unaffected: benches speak S3 to the gateway directly.
