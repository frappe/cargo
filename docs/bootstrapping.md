# Setting up a Cargo host

Cargo runs on its own machine, one per region. This is how a bare VM becomes a Cargo host
that Central can use.

Nothing here is automatic. An operator runs a script on the new machine, then fills in one
form in Central. After that Cargo runs on its own.

## Before you start

Central needs an **Atlas Instance** for the region already. Registration reads Atlas's URL
from it and passes it to Cargo, and it refuses to run if there isn't one.

## Step 1 — run the script on the new machine

```bash
PILOT_ADMIN_PASSWORD=... SITE_PASSWORD=... ./setup.sh
```

Those are two different passwords, and neither is the database password:

| | What it is |
|---|---|
| `PILOT_ADMIN_PASSWORD` | Logs in to pilot's own admin panel on this machine |
| `SITE_PASSWORD` | The Frappe `Administrator` password for the Cargo site |
| MariaDB root | You don't set it. Pilot generates one when it creates the bench. |

`setup.sh` does four things:

1. Runs pilot's installer, which brings Python, Node, MariaDB, Redis and nginx. The machine
   can be completely bare — nothing needs to be installed first. Pilot is pinned to a
   release (`v0.0.29-pre-alpha`) rather than `develop`, so two hosts built weeks apart get
   the same pilot.
2. Creates a bench and a site.
3. Downloads and installs the Cargo app on that site.
4. Creates a user called `central@cargo.local` and prints an API key and secret.

That last step is the one you need:

```
api_key=82bafff47dfb0b3
api_secret=759ed6a8aca5394d239ecc03b4c52b1e
```

This is how **Central logs in to Cargo**. The user carries one role, `Cargo Service`, which
has no desk access — it exists so Central can call two endpoints and nothing more.

You can change the defaults with `PILOT_VERSION`, `BENCH`, `SITE`, `BRANCH` and `REPO`
environment variables. `BRANCH` is Cargo's own branch and still defaults to `develop`.

## Step 2 — tell Central about the machine

In Central, create a **Cargo Instance** and fill in:

- **Region** — which region this Cargo provisions for. One Cargo per region.
- **Base URL** — where the Cargo site is served, e.g. `http://10.0.0.5:8000`.
- **API Key** and **API Secret** — the pair the script printed.

Then press **Register**.

## Step 3 — what Register does

Central does all of this in one go:

1. Mints two tokens for this host (see below).
2. Calls `cargo.api.central.configure` on the Cargo site, logging in with the API key and
   secret, and passes it Central's URL, Atlas's URL, and the two tokens.
3. Cargo saves all of that into its **Cargo Settings**.
4. Central calls back and asks the host how it's doing. Only if the host says it is
   configured does Central mark the instance **Registered** and **Reachable**.

That last check matters. A successful call only proves the request arrived — it doesn't
prove Cargo saved anything. So Central reads the host back before believing it. If the host
says it isn't configured, registration fails and nothing is written, so you can fix the
problem and press Register again from a clean slate.

## The two tokens

Cargo talks to two things, so Central mints two tokens:

| Token | Used for | Audience | Scope |
|---|---|---|---|
| `central_access_token` | calls to Central | `central` | `cargo:central` |
| `atlas_access_token` | calls to Atlas | `atlas` | `cargo:atlas` |

Both are signed by Central. Central checks its own signature; Atlas checks it against
Central's public keys, which anyone can fetch.

They are kept separate on purpose. If Cargo only had one token, a copy stolen from an Atlas
request could be used to ask Central for cluster secrets. With two, the token that opens
Atlas is rejected by Central and the other way around.

Both travel in an `X-Cargo-Token` header rather than the usual `Authorization` header.
That's not a style choice: Frappe treats `Authorization` as either OAuth or an API key, and
rejects anything else with a 401 before the request even reaches the endpoint.

Re-registering mints fresh tokens and overwrites the old ones, so the previous pair stops
working. That is how you cut off a host you no longer trust.

## Checking on a host later

**Test Connection** on the Cargo Instance asks the host how it's doing. It reports whether
the host is reachable, whether it still has its settings, and how many deployments it is
running. Use it when something looks wrong; it changes nothing.

## What Central knows and doesn't

Central stores where each Cargo is and the tokens it issued. It never calls Cargo to do
work — Cargo does the calling. The only time Central reaches out is registration and Test
Connection.

So if a Cargo host is down, provisioning new deployments stops, but everything already
running is unaffected: benches talk to their services directly, and Central talks to those
services directly too.
