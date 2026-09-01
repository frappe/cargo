# Setting up a Cargo host

Cargo runs on its own machine, one per region. This is how a bare VM becomes a Cargo host
that Central trusts.

The host does the work. Central hands out a short-lived token, and the host spends it to
collect the two tokens it runs on. **Central never calls Cargo** — not during setup, not
after. That means a Cargo host does not have to be reachable from Central at all.

## Before you start

Central needs an **Atlas Instance** for the region already, because the host will be calling
Atlas for machines and you have to give it Atlas's URL.

## Step 1 — create the Cargo Instance in Central

Create a **Cargo Instance** and set its **Region**. One Cargo per region. That is the only
field you fill in; everything else on the form is written by the host when it enrols.

The instance starts as **Draft**.

## Step 2 — issue a bootstrapping token

Press **Issue Bootstrapping Token** on that instance. Central shows you one line:

```
CENTRAL_BOOTSTRAPPING_TOKEN=eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...
```

Copy it now. It is shown once, and it **expires in 30 minutes**, so issue it when you are
ready to run the install, not the day before. If it expires, press the button again — a new
token replaces the old one.

The token is a JWT signed by Central. Its audience is this Cargo Instance's name, which is
how Central knows which host is calling later without the host having to claim an identity.

## Step 3 — run the script on the new machine

```bash
PILOT_ADMIN_PASSWORD=... \
SITE_PASSWORD=... \
CENTRAL_URL=https://central.example.com \
ATLAS_URL=https://atlas.example.com \
CENTRAL_BOOTSTRAPPING_TOKEN=... \
./setup.sh
```

`setup.sh` refuses to start unless all five are set.

The two passwords are different things, and neither is the database password:

| | What it is |
|---|---|
| `PILOT_ADMIN_PASSWORD` | Logs in to pilot's own admin panel on this machine |
| `SITE_PASSWORD` | The Frappe `Administrator` password for the Cargo site |
| MariaDB root | You don't set it. Pilot generates one when it creates the bench. |

The script then:

1. Runs pilot's installer, which brings Python, Node, MariaDB, Redis and nginx. The machine
   can be completely bare. Pilot is pinned to a release (`v0.0.29-pre-alpha`) rather than
   `develop`, so two hosts built weeks apart get the same pilot.
2. Creates a bench and a site.
3. Downloads the Cargo app.
4. Exports `CENTRAL_URL`, `ATLAS_URL` and `CENTRAL_BOOTSTRAPPING_TOKEN`, then installs Cargo
   on the site.

Step 4 is where enrolment happens: Cargo's install hook reads those three variables.

`PILOT_VERSION`, `BENCH`, `SITE`, `BRANCH` and `REPO` can be overridden. `BRANCH` is Cargo's
own branch and still defaults to `develop`.

## Step 4 — what the install hook does

`cargo/install.py` runs on `after_install`:

1. Saves `CENTRAL_URL`, `ATLAS_URL` and the bootstrapping token into **Cargo Settings**.
2. Calls Central, presenting the bootstrapping token, and sends its own base URL
   (`frappe.utils.get_url()`) so Central knows where this host lives.
3. Saves the two access tokens Central returns.
4. Clears the bootstrapping token — it has been spent.

Central, on its side, saves the same two tokens, records the base URL, sets the instance to
**Registered** with a timestamp, and clears its copy of the bootstrapping token.

If any of that fails, the install fails, and you start again with a fresh token. Nothing is
half-written on either side: a host without both access tokens can do nothing, and an
instance that is still **Draft** never got them.

### Installing without enrolling

If **none** of the three variables are set, the hook does nothing and the install succeeds.
That is CI, and a local dev site.

If **some** of them are set, the install fails loudly. A half-supplied set is a typo, not an
intention.

## The one-time token

The bootstrapping token can only be spent once. Central stores the token it issued, and on
enrolment compares the presented token against the stored one; a successful enrolment clears
it. So a replay — the same token used a second time — is rejected with a 401.

That's the point: a token leaked from a shell history or a log can't be used to collect a
second set of credentials for a host that already enrolled.

## The two tokens the host runs on

Cargo talks to two upstreams, so Central mints two tokens:

| Token | Used for | Audience | Scope |
|---|---|---|---|
| `central_access_token` | calls to Central | `central` | `cargo:central` |
| `atlas_access_token` | calls to Atlas | `atlas` | `cargo:atlas` |

Both are signed by Central. Central checks its own signature; Atlas checks it against
Central's published public keys.

They are separate on purpose. If Cargo carried one token, a copy captured from an Atlas
request could be turned around and used to ask Central for cluster secrets. With two, the
scope check rejects the Atlas token at Central and the other way round.

Both ride an `X-Cargo-Token` header, not `Authorization`. That is not a style choice: Frappe
treats `Authorization` as OAuth or an API key and rejects anything else with a 401 before
the request reaches the endpoint. The bootstrapping token rides
`X-Cargo-Bootstrapping-Token` for the same reason.

These tokens are long-lived — a year. Cargo is infrastructure, not a session.

## Re-enrolling a host

Press **Re-issue Bootstrapping Token** on the instance and run the enrolment again. Fresh
tokens overwrite the old pair, so the previous tokens stop working. That is how you cut off
a host you no longer trust.

## What Central knows and doesn't

Central stores each Cargo's region, base URL, and the tokens it issued. The base URL is a
record of where the host said it was — Central does not call it. All traffic runs the other
way: Cargo asks Central for cluster secrets, and tells Central when a cluster is up or has
failed.

So if a Cargo host is down, provisioning new clusters stops, but everything already running
is unaffected: benches talk to their services directly, and Central talks to those services
directly too.
