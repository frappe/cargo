# Cargo certificate expiring

Caddy on the object storage gateway or the Datum host obtains and renews the certificate of each public name itself. This alert means that a certificate has fewer than 14 days left, or that it does not verify for its name. The alert detail names the host.

1. Run `echo | openssl s_client -connect <host>:443 -servername <host> 2>/dev/null | openssl x509 -noout -subject -enddate` to see the certificate that is served.
2. Sign in to the gateway or the Datum host and read `journalctl -u caddy -n 200` for ACME errors.
3. If the DNS record points to another address, select **Update DNS Records** on the Object Storage Cluster or the Datum Server. Caddy then retries the renewal.
4. Run `systemctl reload caddy` after you fix the cause, and check the certificate again.
