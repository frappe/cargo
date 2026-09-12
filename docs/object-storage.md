# Object storage

## Purpose

Each region runs one Garage cluster, and Cargo owns it. A cluster is one gateway machine and the storage machines behind it. Benches speak S3 to the gateway; Central asks Cargo for buckets and keys. This document covers the cluster's lifecycle. For what the cluster reports about itself once it runs, read [cluster health](health.md).

## Records

An **Object Storage Cluster** is one Garage cluster: the machines it holds, the secrets they run on, and the shape of its layout. Its **Status** is the build lifecycle, and its **Health** is what the cluster is doing now. The two never mean the same thing.

| Status | Meaning |
|---|---|
| Draft | It has machines, or is waiting for them. Nothing is installed. |
| Setting Up | A setup run owns the cluster. |
| Active | The nodes joined, the layout is applied, and the cluster can hold an object. |
| Failed | The last run could not bring up enough nodes. See Error. |

Each machine of a cluster is a **Machine**, named after the cluster and its role, such as `OSC-0001-storage-0001`. Cargo permits one Active cluster per region. Other clusters can stay for history, but setup stops while another is Active.

## Requirements

Cargo Settings must carry `region`, `wildcard_domain`, `atlas_url`, `atlas_token`, `central_url`, `central_webhook_secret`, `proxy_url`, and `proxy_token`. A cluster cannot be inserted without the Central values, because it wires its own webhook when it is created.

`base_image` is empty by default, which means Cargo asks Atlas for the Ubuntu system image. Atlas names an image by a generated id, so a name typed into this field will not be found. Leave it empty unless you have an id.

## Setting a cluster up by hand

1. Create an Object Storage Cluster. Cargo mints its `rpc_secret`, `admin_token`, and `metrics_token`, and points a webhook at Central.
2. Select **Add > Gateway Node** and give it a size. Cargo asks Atlas for the machine.
3. Select **Add > Storage Node** once for each storage machine. A cluster needs at least `replication_factor` of them.
4. Wait. `sync_pending_machines` moves each machine to Running when Atlas reports it up with a mesh address.
5. Select **Set Up Cluster**.

Setup installs Garage on each machine that has not joined, gateway first, records the peers, applies the layout, puts nginx in front of the gateway, and publishes the `s3-svc` and `s3-admin-svc` routes to the Proxy. It is idempotent: setting up again picks up whatever has not joined, so it is also how a failed run is retried. The machines are kept on a failure, because releasing them is the operator's call.

## Auto spawn

Cargo can bring the cluster up on its own. Put `default_storage_cluster_config` in the site config:

```json
"default_storage_cluster_config": {
  "storage_node_count": 3,
  "replication_factor": 3,
  "gateway": {"cpu": 2, "ram_gb": 4, "disk_gb": 20},
  "storage": {"cpu": 2, "ram_gb": 4, "disk_gb": 100}
}
```

Every value is required, and each must be a whole number of at least 1. `storage_node_count` must be at least `replication_factor`, because a cluster with fewer storage nodes than a full copy needs is one every setup run refuses. The cluster takes its replication factor from here, so the two can never disagree.

An unusable config is logged once and nothing is built. Without the key, Cargo builds nothing and the desk flow above is the only way in.

`ensure_cluster` then runs on the scheduler under a site lock, so two runs never rent machines at the same time. On each run it:

1. Builds a cluster if the region has none. It builds one only when there is no Object Storage Cluster at all, so a cluster added by hand is never joined by a second, and a Failed cluster is retried rather than replaced. This is what makes a leaked machine impossible: Cargo never rents a second set while the first is still held.
2. Asks Atlas for the machines the cluster is short of, gateway first. The count is a deficit, so a run that fails part way asks only for what is missing next time, and a full cluster is asked for nothing.
3. Sets the cluster up when every machine is Running.

Each machine is committed as it is built. `Machine.request` rolls back the row it failed on, and an uncommitted sibling would roll back a machine Atlas had already built.

A cluster Cargo built carries **Auto Spawn**. Only such a cluster is advanced on its own.

### What stops it

A machine that comes up Broken stops the run. Cargo records which machine on the cluster and does nothing else: it does not terminate the machine, and it does not ask for a replacement. Replacing a machine unattended is how a spawner runs away with money, and a machine that would not boot is worth reading the Error Log for. Select **Release Machines** for it, and the next run asks Atlas for another.

A failed setup run is tried again, up to 3 runs, counted in **Auto Setup Attempts**. Setting up again rents no machine, so a slow sshd, an apt mirror, or a Proxy that blinked is worth another run. This is the opposite of what release tracking does with a failed image, and for a reason: each image retry rents a fresh build machine, so retrying one hourly would be unbounded spend. Here it costs an SSH session.

A run that is under way owns the cluster, so a cluster in Setting Up is never touched. The next attempt can only start once the run before it has finished, which is what paces the budget rather than the scheduler.

The counter clears when the cluster serves, so a fault you fix arms automatic setup again. It is never spent on a cluster that has already served: such a cluster fails because its machines died, and setting up again cannot raise the dead.

## Reporting to Central

The cluster reports itself. A Frappe Webhook fires on Active and on Failed, which are the two states worth a call. Read [what Cargo and Central say to each other](central-contract.md).

## Validation

Run the object storage tests:

```bash
bench --site <site> run-tests --module cargo.object_storage.test_spawn
```
