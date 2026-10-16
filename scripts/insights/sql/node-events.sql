WITH
anchor AS (SELECT max(ts) AS at FROM samples WHERE metric = 'garage_cluster_healthy'),
window_samples AS (
  SELECT * FROM samples
  WHERE metric IN ('garage_cluster_healthy', 'garage_api_admin_request_counter', 'garage_cluster_layout_node_disconnected_time', 'garage_rpc_request_counter')
    AND ts >= (SELECT at FROM anchor) - INTERVAL 168 HOUR - INTERVAL 10 MINUTE
),
nodes AS (
  SELECT labels['cluster'] AS cluster, labels['machine'] AS machine, any(labels['role']) AS role,
    anyIf(labels['from'], metric = 'garage_rpc_request_counter') AS node_id
  FROM window_samples GROUP BY cluster, machine
),
ticks AS (SELECT DISTINCT labels['cluster'] AS cluster, ts FROM window_samples WHERE metric = 'garage_cluster_healthy'),
own AS (
  SELECT machine, ts, healthy, 1 AS present,
    position > 1 AND scrapes < previous_scrapes AS restarted, previous_ts
  FROM (
    SELECT machine, ts, healthy, scrapes,
      lagInFrame(scrapes) OVER w AS previous_scrapes, lagInFrame(ts) OVER w AS previous_ts, row_number() OVER w AS position
    FROM (
      SELECT labels['machine'] AS machine, ts,
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
  SELECT labels['cluster'] AS cluster, labels['id'] AS node_id, ts, max(value) AS disconnected_seconds,
    arrayStringConcat(groupUniqArray(labels['machine']), ', ') AS seen_by
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
SELECT * FROM (
SELECT ts, cluster, machine, role,
  multiIf(level = 3, 'Restarted', level = 2, 'No data', 'Disconnected') AS event,
  multiIf(
    level = 3, concat('Garage counters reset; restarted after ', toString(previous_ts)),
    level = 2, 'Scrape failed or node unreachable',
    concat('Peers lost it for ', toString(disconnected_seconds), 's, seen by ', seen_by)
  ) AS detail
FROM grid
WHERE level > 0
UNION ALL
SELECT ts, cluster, machine, role, 'Cluster unhealthy' AS event, 'At least one node reported garage_cluster_healthy = 0' AS detail
FROM cluster_grid WHERE level > 0
)
ORDER BY ts DESC
