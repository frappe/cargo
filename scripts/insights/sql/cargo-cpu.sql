SELECT toStartOfMinute(ts) AS time, if(metric = 'system_cpu_usage_percent', 'busy', labels['mode']) AS series, avg(value) AS value
FROM samples
WHERE resource_id LIKE 'cargo:%' AND ts > now() - INTERVAL 24 HOUR
  AND (metric = 'system_cpu_usage_percent' OR (metric = 'system_cpu_mode_percent' AND labels['mode'] = 'iowait'))
GROUP BY time, series
