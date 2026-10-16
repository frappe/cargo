WITH latest AS (
  SELECT labels['cluster'] AS cluster, labels['region'] AS region, labels['machine'] AS machine, metric, argMax(value, ts) AS value, max(ts) AS last_seen
  FROM samples
  WHERE metric IN ('garage_cluster_healthy', 'garage_cluster_available', 'garage_cluster_storage_nodes_ok', 'garage_cluster_storage_nodes', 'garage_cluster_partitions_quorum', 'garage_cluster_partitions', 'garage_cluster_connected_nodes', 'garage_cluster_known_nodes')
    AND ts > now() - INTERVAL 15 MINUTE
  GROUP BY cluster, region, machine, metric
)
SELECT
  cluster,
  any(region) AS region,
  minIf(value, metric = 'garage_cluster_healthy') AS healthy,
  minIf(value, metric = 'garage_cluster_available') AS available,
  minIf(value, metric = 'garage_cluster_storage_nodes_ok') AS storage_nodes_ok,
  maxIf(value, metric = 'garage_cluster_storage_nodes') AS storage_nodes,
  minIf(value, metric = 'garage_cluster_partitions_quorum') AS partitions_quorum,
  maxIf(value, metric = 'garage_cluster_partitions') AS partitions,
  minIf(value, metric = 'garage_cluster_connected_nodes') AS connected_nodes,
  maxIf(value, metric = 'garage_cluster_known_nodes') AS known_nodes,
  uniqExact(machine) AS reporting_machines,
  max(last_seen) AS last_seen
FROM latest
GROUP BY cluster
