# Object storage

## Purpose

Each region runs one Garage cluster, and Cargo owns it. A cluster is one or more gateway machines and the storage machines behind them. The first machine added is always a gateway. DNS names every running gateway, and benches speak S3 to whichever answers; Central asks Cargo for buckets and keys. This document covers the cluster's lifecycle. For what the cluster reports about itself once it runs, read [cluster health](health.md).

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

Cargo Settings must carry `region`, `wildcard_domain`, `atlas_url`, `atlas_token`, `central_url`, and `central_webhook_secret`. A cluster cannot be inserted without the Central values, because it wires its own webhook when it is created.

Each new machine boots from the Ubuntu system image that Atlas serves when the machine is requested, so a retired image does not block new nodes.

## Setting a cluster up by hand

1. Create an Object Storage Cluster. Cargo mints its `rpc_secret`, `admin_token`, and `metrics_token`, and points a webhook at Central.
2. Select **Add > Gateway Node** and give it a size. Cargo asks Atlas for the machine.
3. Select **Add > Storage Node** once for each storage machine. A cluster needs at least `replication_factor` of them.
4. Wait. `sync_all_machines` moves each machine to Running when Atlas reports it up with a mesh address.
5. Select **Set Up Cluster**.

Setup installs Garage on each machine that has not joined, gateway first, records the peers, applies the layout, puts nginx on every gateway, and waits until `s3-svc` and `s3-admin-svc` answer over HTTPS. Every gateway must run and have a public IPv4 from Atlas. Setup replaces each gateway's Atlas firewall, installs nginx routed to that gateway's own Garage, then asks Atlas to point both names at every gateway's public address, each with a Route53 health check, and their `internal-` names at their mesh addresses. You do not set DNS by hand. To publish the records again without a full setup, select **Update DNS Records**. nginx serves the certificate files at `/etc/nginx/tls/<name>.crt` and `.key`, and setup fails until they exist; Cargo does not obtain certificates yet. Garage listens on each node's mesh address only, so the public address reaches nginx alone. nginx allows no origins of its own: Garage answers CORS for each bucket, as [browser access](#browser-access) explains. It is idempotent: setting up again picks up whatever has not joined, so it is also how a failed run is retried. The machines are kept on a failure, because releasing them is the operator's call.

When setup applies the layout, it also removes each Garage node that no machine of the cluster holds: the node of a released machine, and the old node of a machine that came back with a new node id after it lost its metadata. The old node is removed only once the new one is up, so a machine that is down keeps its role. On every run, if Garage still waits for nodes that are down and no longer in the layout, and no node in the layout is down, setup tells Garage to stop waiting for them (`ClusterLayoutSkipDeadNodes`). A skip that failed or had to wait is tried again on the next run. Garage refuses this when the remaining nodes do not hold a full copy. Run setup after you release a machine or replace a node.

## Browser access

A browser can call a bucket directly, with a presigned URL, only from a website the bucket allows. Each **Bucket** keeps its allowed websites in **Allowed Origins**. An origin is a scheme, a host and an optional port, such as `https://files.example.com`. It has no path and no wildcard. Cargo stores each origin as a browser sends it in the `Origin` header: lowercase, with no default port and no trailing slash.

Cargo writes the origins to Garage as the bucket's CORS rules, through the admin API (`UpdateBucket`). S3's own `PutBucketCors` needs a key with owner permission, and the keys Cargo issues are read and write only. Each rule allows `GET`, `HEAD`, `PUT` and `POST` with any request header, and lets the browser read `ETag`. Cargo writes one rule for each origin, because Garage names every origin of a matching rule in one `Access-Control-Allow-Origin` header, and a browser refuses a header that names more than one.

The origin of `central_url` is always added, so the Central dashboard can upload and download on every bucket. It is not stored on the bucket. Cargo writes the rules when it creates a bucket and when the allowed origins change. Buckets are only changed through Cargo, so the rules cannot drift. The `apply_bucket_cors_rules` patch wrote them once for the buckets made before this.

nginx passes every request through, the unsigned preflight `OPTIONS` among them. Garage reads the bucket from the path and answers from that bucket's rules: it refuses a preflight from any other origin with a 403. The signature still authorizes each request. CORS only lets the browser send it and read the answer.

Garage before v2.3.0 accepts CORS rules on `UpdateBucket` and ignores them. Cargo reads each node's version from the cluster status first:

- Setup is refused before it changes anything when a node runs an older Garage, so the cluster keeps its status and its old proxy. Upgrade Garage on every node, then set up again. Setup installs Garage only on a machine that has not joined, so changing the cluster's **Version** does not upgrade the nodes that joined.
- A change to a bucket's allowed origins is refused with the version found.
- A new bucket is still created, without rules. Its cluster still runs the old Caddy, which answers CORS for the dashboard. After upgrading Garage, set the bucket's allowed origins again to write them.

Garage v2.3.0 sends neither `Access-Control-Max-Age` nor `Vary: Origin`, so nginx adds both on `s3-svc`: `Max-Age: 3600` on preflight answers, and `Vary: Origin` on every answer, since `Access-Control-Allow-Origin` now depends on the caller.

## Releasing a machine

Select **Release Machines** to give machines back to Atlas. A machine that is not joined is terminated at once, unless Garage is still moving data for an older layout version. A storage node that joined, and every machine while Garage is moving data, is drained first:

1. Cargo removes its node from the Garage layout and applies the layout. Garage starts to move its data to the other nodes.
2. Cargo waits until no layout version is Draining. It checks about once a minute. If the node is already dead, Cargo tells Garage to stop waiting for it.
3. Cargo waits until no running node has blocks in its resync queue or blocks it failed to fetch. Garage does not include blocks in the Draining status. A node that does not answer counts as not done.
4. Cargo terminates the machine and removes its row.

Cargo refuses the release when the joined storage nodes that stay are fewer than `replication_factor`, and it never releases the last gateway, so a cluster always keeps one. When a gateway stops answering, Route53 leaves it out and admin calls skip it. When it dies or is released, Cargo takes it out of DNS; the cluster fails only when every gateway is dead. To add a gateway, select **Add > Gateway Node**, then set up again. To replace a storage node, add the new node, run setup, then release the old one. Setup and release cannot run at the same time.

## Auto spawn

Cargo can bring the cluster up on its own. Put `default_storage_cluster_config` in the site config:

```json
"default_storage_cluster_config": {
  "storage_node_count": 3,
  "replication_factor": 3,
  "gateway": {"cpu_millicores": 2000, "ram_gb": 4, "disk_gb": 20},
  "storage": {"cpu_millicores": 2000, "ram_gb": 4, "disk_gb": 100}
}
```

Every value is required, and each must be a whole number of at least 1. The optional `gateway_node_count` sets how many gateways the cluster keeps, and defaults to 1. `storage_node_count` must be at least `replication_factor`, because a cluster with fewer storage nodes than a full copy needs is one every setup run refuses. The cluster takes its replication factor from here, so the two can never disagree.

An unusable config is logged once and nothing is built. Without the key, Cargo builds nothing and the desk flow above is the only way in.

`ensure_cluster` then runs on the scheduler under a site lock, so two runs never rent machines at the same time. On each run it:

1. Builds a cluster if the region has none. It builds one only when there is no Object Storage Cluster at all, so a cluster added by hand is never joined by a second, and a Failed cluster is retried rather than replaced. This is what makes a leaked machine impossible: Cargo never rents a second set while the first is still held.
2. Asks Atlas for the machines the cluster is short of, gateway first. The count is a deficit, so a run that fails part way asks only for what is missing next time, and a full cluster is asked for nothing.
3. Sets the cluster up when every machine is Running.

Each machine is committed as it is built. `Machine.request` rolls back the row it failed on, and an uncommitted sibling would roll back a machine Atlas had already built.

A cluster Cargo built carries **Auto Spawn**. Only such a cluster is advanced on its own.

### What stops it

A machine that comes up Broken stops the run. Cargo records which machine on the cluster and does nothing else: it does not terminate the machine, and it does not ask for a replacement. Replacing a machine unattended is how a spawner runs away with money, and a machine that would not boot is worth reading the Error Log for. Select **Release Machines** for it, and the next run asks Atlas for another.

A failed setup run is tried again, up to 3 runs, counted in **Auto Setup Attempts**. Setting up again rents no machine, so a slow sshd, an apt mirror, or a certificate that was slow to issue is worth another run. This is the opposite of what release tracking does with a failed image, and for a reason: each image retry rents a fresh build machine, so retrying one hourly would be unbounded spend. Here it costs an SSH session.

A run that is under way owns the cluster, so a cluster in Setting Up is never touched. The next attempt can only start once the run before it has finished, which is what paces the budget rather than the scheduler.

The counter clears when the cluster serves, so a fault you fix arms automatic setup again. It is never spent on a cluster that has already served: such a cluster fails because its machines died, and setting up again cannot raise the dead.

## Reporting to Central

The cluster reports itself. A Frappe Webhook fires on Active and on Failed, which are the two states worth a call. Read [what Cargo and Central say to each other](central-contract.md).

## Validation

Run the object storage tests:

```bash
bench --site <site> run-tests --module cargo.object_storage.test_spawn
```
