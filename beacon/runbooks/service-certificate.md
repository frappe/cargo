# Service certificate expiring

nginx on the object storage gateways and the Datum host serves the certificate files under `/etc/nginx/tls`. Nothing renews them yet. This alert means that a certificate has fewer than 14 days left, or that it does not verify for its name. The alert detail names the host.

1. Run `echo | openssl s_client -connect <host>:443 -servername <host> 2>/dev/null | openssl x509 -noout -subject -enddate` to see the certificate that is served.
2. Sign in to the gateway or the Datum host and check the expiry of the files under `/etc/nginx/tls` with `openssl x509 -noout -enddate -in <file>`.
3. If the DNS record points to another address, select **Update DNS Records** on the Object Storage Cluster or the Datum Server. Then replace the certificate files on every gateway or the Datum host.
4. Run `nginx -t && systemctl reload nginx` after you fix the cause, and check the certificate again.
