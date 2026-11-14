WITH
machines AS (
  SELECT labels['machine'] AS machine, argMax(labels['cluster'], ts) AS cluster, argMax(labels['role'], ts) AS role
  FROM samples
  WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] != 'cargo' AND ts > now() - INTERVAL 7 DAY
  GROUP BY machine
),
latest AS (
  SELECT m.cluster AS cluster, m.machine AS machine, s.metric AS metric, argMax(s.value, s.ts) AS value, max(s.ts) AS last_seen
  FROM samples AS s
  INNER JOIN machines AS m ON m.machine = s.resource_id
  WHERE s.metric IN ('garage_cluster_healthy', 'garage_cluster_available', 'garage_cluster_storage_nodes_ok', 'garage_cluster_storage_nodes', 'garage_cluster_partitions_quorum', 'garage_cluster_partitions', 'garage_cluster_connected_nodes', 'garage_cluster_known_nodes')
    AND s.ts > now() - INTERVAL 15 MINUTE
  GROUP BY cluster, machine, metric
)
SELECT
  cluster,
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
