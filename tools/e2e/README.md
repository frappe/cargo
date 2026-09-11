# Proving setup.sh on a bare machine

`setup.sh` claims a bare Ubuntu box becomes a running Cargo host. This runs that claim
against one, from pilot's own installer to a site answering over nginx.

The container runs **systemd as PID 1**, because that is what pilot deploys the bench
with. A plain container has no init, so `pilot setup production` has nothing to hand the
bench to and the run proves nothing.

```bash
tools/e2e/run.sh
```

Roughly fifteen minutes on a cold cache: the installer builds MariaDB, Redis, nginx, Node
and a Python environment before Cargo is even downloaded.

## What it checks

1. `setup.sh` creates the bench user, and the installer's root pass prepares the host.
2. Its second pass installs pilot for that user.
3. `pilot new` writes the bench, `init` builds it, `new-site` creates the site, `get-app`
   downloads Cargo.
4. Production comes up: systemd units for the workload, nginx in front.
5. Cargo installs, and its hook writes Cargo Settings from the environment.
6. The setup wizard is complete, so the desk is reachable rather than stuck.
7. nginx answers on port 80 and the site responds.

Fifteen checks in all, from `tools/e2e/verify.sh`.

## Leaving it running

It publishes the container's nginx on port 8200, so both the site and pilot's admin panel
are reachable while it runs — one nginx serves both and the Host header picks between them:

| | |
|---|---|
| Cargo site | http://cargo.localhost:8200 |
| Pilot admin | http://pilot.cargo.localhost:8200 |

Set `HTTP_PORT` to publish somewhere else. Browsers resolve `*.localhost` themselves, so
there is nothing to add to `/etc/hosts`.

`tools/e2e/run.sh --keep` leaves the container up so you can look around:

```bash
docker exec -it cargo-e2e bash
docker exec -it -u frappe cargo-e2e bash -lc "pilot -b cargo status"
```

Nothing here talks to a real Central or Atlas. The URLs point at stubs, which is enough
to prove the install: Cargo makes no outbound call while installing.
