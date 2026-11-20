# Service certificate failed or expiring

nginx on the object storage gateways and the Datum host serves the certificate files under `/etc/nginx/tls`. Cargo issues each one as a **Service Certificate** (`object-storage` or `telemetry`) from Let's Encrypt over dns-01, and a daily job re-issues it within 30 days of expiry and copies it to every machine. Two alerts use this runbook: the certificate could not be issued (Critical), or the served certificate has fewer than 14 days left or does not verify for its name (Medium). The alert detail names the certificate or the host.

## Could not be issued

1. In Cargo Desk, open the Error Log and find "Unable to renew the <name> certificate", or the setup that failed. The traceback says whether Let's Encrypt refused the order or Atlas refused the `_acme-challenge` TXT record.
2. Fix the cause: a missing **Let's Encrypt Email** in Cargo Settings, a rate limit from Let's Encrypt, or an Atlas DNS error.
3. Issue it again: select **Set Up Again** on the Object Storage Cluster or the Datum Server, or run `frappe.get_doc("Service Certificate", "<name>").renew()` in the console. The alert resolves once the status is Active.

## Expiring or not verifying

1. Run `echo | openssl s_client -connect <host>:443 -servername <host> 2>/dev/null | openssl x509 -noout -subject -enddate` to see the certificate that is served.
2. Compare it with **Expires On** of the Service Certificate. If the stored one is newer, a machine missed the copy: run `certificate = frappe.get_doc("Service Certificate", "<name>"); certificate.proxy.add_certificate_to_machines(certificate)` in the console, or select **Set Up Again**.
3. If the stored one is also expiring, its renewal failed: follow the section above.
4. If the DNS record points to another address, select **Update DNS Records** on the Object Storage Cluster or the Datum Server.
