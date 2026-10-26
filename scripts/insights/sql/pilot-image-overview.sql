WITH
batch AS (
  SELECT max(ts) AS at FROM samples
  WHERE resource_id LIKE 'cargo:%' AND metric = 'cargo_pilot_image_latest' AND ts > now() - INTERVAL 7 DAY
),
current AS (
  SELECT metric, value, labels FROM samples
  WHERE resource_id LIKE 'cargo:%' AND metric LIKE 'cargo_pilot_image_%' AND ts = (SELECT at FROM batch)
)
SELECT
  (SELECT dateDiff('minute', at, now()) / 60 FROM batch) AS hours_since_report,
  (SELECT countIf(metric = 'cargo_pilot_image_latest' AND labels['status'] = 'Completed') FROM current) AS completed,
  (SELECT countIf(metric = 'cargo_pilot_image_latest' AND labels['status'] = 'Failed') FROM current) AS failed,
  (SELECT countIf(metric = 'cargo_pilot_image_in_progress_seconds') FROM current) AS in_progress,
  (SELECT countIf(metric = 'cargo_pilot_image_in_progress_seconds' AND value > 3600) FROM current) AS stuck,
  (SELECT countIf(metric = 'cargo_pilot_image_failed_attempts') FROM current) AS failures_7d
