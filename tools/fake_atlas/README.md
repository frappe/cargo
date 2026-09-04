# fake atlas

> **Test scaffolding.** Vibe-coded in an afternoon so the image builder could be tested
> locally. Not reviewed, not hardened. Use it for local testing and CI, nothing else.

Cargo asks Atlas for machines. This pretends to be Atlas and gives it Docker containers
instead. It answers the same URLs with the same JSON, so **you never have to change Cargo** —
point Cargo Settings' Atlas URL at this and press Build.

## Running it

```bash
python3 tools/fake_atlas/fake_atlas.py --port 8100 --systemd
```

Use `--systemd` for anything real. Containers then boot properly and can start services,
which pilot's installer and Garage both need. Without it you get a bare container with only
SSH — fine for checking that machines are created, reached, snapshotted and destroyed, but
anything calling `systemctl` will fail.

## What it does

| Cargo asks for | this does |
|---|---|
| machines | starts one container per machine the request asks for, in the same order |
| a machine's status | says `Pending` until SSH is up, then `Running` with an address |
| a snapshot | `docker commit`, tagged `cargo-snapshot/<name>` |
| a snapshot's status | `docker image inspect` |
| a machine thrown away | `docker rm -f` |

## Addresses, and the two bits of setup

Machines are called `cargo-vm1`, `cargo-vm2` and so on, and that name is what Cargo is given
as the machine's address. Names beat container addresses here because a Mac cannot reach a
container address at all, and because the names stay the same every run, so you set this up
once and forget it.

**One.** Point the names at your own machine, in `/etc/hosts`:

```
127.0.0.1 cargo-vm1 cargo-vm2 cargo-vm3 cargo-vm4 cargo-vm5 cargo-vm6 cargo-vm7 cargo-vm8 cargo-vm9 cargo-vm10 cargo-vm11 cargo-vm12
```

The banner prints this line when the server starts, so you can copy it from there.

**Two.** Let SSH read the file this server writes, by adding one line to `~/.ssh/config`:

```
Include ~/.ssh/config.d/fake-atlas
```

Now every way Cargo reaches a machine ends up somewhere real:

| Cargo does | where it lands |
|---|---|
| SSH to `cargo-vm1` | `/etc/hosts` sends it to your machine, the SSH file sends it to port 2222 |
| HTTP to `cargo-vm1:3903` | your machine, where the container publishes Garage's admin API |
| tells Garage a node is at `cargo-vm1` | inside Docker, where containers look each other up by name |

The last one is why the containers share a network of their own: Docker only lets containers
find each other by name there. Cargo sees one address and knows nothing about any of this.

## What it cannot tell you

Green here does not mean green in production:

- **Central can't reach the cluster.** Cargo tells Central where the cluster is, using a
  name that only means something on your machine. Building a cluster works; Central using
  it does not.
- **Only one cluster at a time.** The gateway publishes Garage's admin API on port 3903 of
  your machine, so a second cluster's gateway cannot start while the first is running.
- **A snapshot is only a copy of the files.** It says nothing about whether the image boots.
- **Machines are not really spread out.** The request says which machines should sit apart;
  this only counts them.
- **Any token is accepted.** There is no real check.

## Clearing up afterwards

```bash
docker rm -f $(docker ps -aq --filter name=cargo-fake)
docker network rm cargo-fake
docker image ls "cargo-snapshot/*"
```
