SELECT max(ts) AS last_seen, service, level, source,
  substring(replaceRegexpAll(replaceRegexpAll(splitByChar('\n', message)[1], '\\|\\|[0-9a-z]{10}\\|\\|', '||…||'), '\\d{4}-\\d{2}-\\d{2}[ T][0-9:.,]+', '…'), 1, 300) AS message,
  count() AS count
FROM logs
WHERE resource_id LIKE 'cargo:%' AND level NOT IN ('info', 'debug') AND ts > now() - INTERVAL 7 DAY
GROUP BY service, level, source, message
ORDER BY last_seen DESC
LIMIT 200
