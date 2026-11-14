WITH
anchor AS (SELECT max(ts) AS at FROM samples WHERE metric = 'garage_cluster_healthy'),
machines AS (
  SELECT labels['machine'] AS machine, argMax(labels['cluster'], ts) AS cluster, argMax(labels['role'], ts) AS role
  FROM samples
  WHERE resource_id = 'cargo' AND metric = 'cargo_machine' AND labels['role'] != 'cargo' AND ts > now() - INTERVAL 7 DAY
  GROUP BY machine
),
window_samples AS (
  SELECT s.ts AS ts, s.metric AS metric, s.labels AS labels, s.value AS value, m.cluster AS cluster, m.machine AS machine, m.role AS role
  FROM samples AS s
  INNER JOIN machines AS m ON m.machine = s.resource_id
  WHERE s.metric IN ('garage_cluster_healthy', 'garage_api_admin_request_counter', 'garage_cluster_layout_node_disconnected_time', 'garage_rpc_request_counter')
    AND s.ts >= (SELECT at FROM anchor) - INTERVAL 168 HOUR - INTERVAL 10 MINUTE
),
nodes AS (
  SELECT cluster, machine, role, anyIf(labels['from'], metric = 'garage_rpc_request_counter') AS node_id
  FROM window_samples GROUP BY cluster, machine, role
),
ticks AS (SELECT DISTINCT cluster, ts FROM window_samples WHERE metric = 'garage_cluster_healthy'),
own AS (
  SELECT machine, ts, healthy, 1 AS present,
    position > 1 AND scrapes < previous_scrapes AS restarted, previous_ts
  FROM (
    SELECT machine, ts, healthy, scrapes,
      lagInFrame(scrapes) OVER w AS previous_scrapes, lagInFrame(ts) OVER w AS previous_ts, row_number() OVER w AS position
    FROM (
      SELECT machine, ts,
        anyIf(value, metric = 'garage_cluster_healthy') AS healthy,
        anyIf(value, metric = 'garage_api_admin_request_counter' AND labels['api_endpoint'] = '/metrics') AS scrapes
      FROM window_samples
      WHERE metric = 'garage_cluster_healthy' OR (metric = 'garage_api_admin_request_counter' AND labels['api_endpoint'] = '/metrics')
      GROUP BY machine, ts
    )
    WINDOW w AS (PARTITION BY machine ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
  )
),
peer AS (
  SELECT cluster, labels['id'] AS node_id, ts, max(value) AS disconnected_seconds,
    arrayStringConcat(groupUniqArray(machine), ', ') AS seen_by
  FROM window_samples
  WHERE metric = 'garage_cluster_layout_node_disconnected_time' AND value > 0
  GROUP BY cluster, node_id, ts
),
grid AS (
  SELECT t.ts AS ts, n.cluster AS cluster, n.machine AS machine, n.role AS role,
    o.present AS present, o.restarted AS restarted, o.previous_ts AS previous_ts, o.healthy AS healthy,
    p.disconnected_seconds AS disconnected_seconds, p.seen_by AS seen_by,
    multiIf(o.restarted, 3, o.present = 0, 2, p.disconnected_seconds > 0, 1, 0) AS level
  FROM ticks AS t
  INNER JOIN nodes AS n ON n.cluster = t.cluster
  LEFT JOIN own AS o ON o.machine = n.machine AND o.ts = t.ts
  LEFT JOIN peer AS p ON p.cluster = n.cluster AND p.node_id = n.node_id AND p.ts = t.ts
  WHERE t.ts >= (SELECT at FROM anchor) - INTERVAL 168 HOUR
),
cluster_grid AS (
  SELECT ts, cluster, concat(cluster, ' cluster') AS machine, 'cluster' AS role,
    if(minIf(healthy, present = 1) < 1, 1, 0) AS level
  FROM grid GROUP BY ts, cluster
)
, last_tick AS (SELECT cluster, max(ts) AS at FROM ticks GROUP BY cluster)
SELECT g.cluster AS cluster, g.machine AS machine, g.role AS role, any(n.node_id) AS node_id,
  if(maxIf(g.present, g.ts = l.at) = 1, 1, 0) AS reporting,
  maxIf(g.ts, g.present = 1) AS last_seen,
  if(countIf(g.restarted) = 0, NULL, maxIf(g.ts, g.restarted)) AS last_restart,
  countIf(g.restarted) AS restarts_7d,
  countIf(g.present = 0) AS missed_scrapes_7d,
  countIf(g.disconnected_seconds > 0) AS disconnects_7d
FROM grid AS g
INNER JOIN last_tick AS l ON l.cluster = g.cluster
INNER JOIN nodes AS n ON n.machine = g.machine
GROUP BY cluster, machine, role
