#!/usr/bin/env python3
"""Route custom domains for this bench through Frappe Central."""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

WILDCARD_DOMAINS = ["__WILDCARD_DOMAIN__"]
PROXY_SERVERS = ["__PROXY_SUBNET__"]

# The edge terminates TLS for a wildcard hostname. Any other domain keeps its TLS here.
SITE_ROUTE = {
	"public_scheme": "https",
	"origin_scheme": "http",
	"client_ip_source": "x_forwarded_for",
}
DOMAIN_ROUTE = {
	"public_scheme": "https",
	"origin_scheme": "https",
	"client_ip_source": "proxy_protocol_v2",
}

METADATA = "http://169.254.169.254/latest"
TIMEOUT_SECONDS = 30
# Cloudflare refuses the default Python-urllib user agent with HTTP 403.
USER_AGENT = "bench-domain-provider/1"


def read_metadata(path: str, headers: dict, method: str = "GET") -> str:
	request = urllib.request.Request(f"{METADATA}/{path}", headers=headers, method=method)
	with urllib.request.urlopen(request, timeout=2) as response:
		return response.read().decode()


def credentials() -> tuple[str, str]:
	"""The Central endpoint and token this VM was handed at boot."""
	token = read_metadata("api/token", {"X-metadata-token-ttl-seconds": "21600"}, "PUT")
	attributes = read_metadata("meta-data/attributes/pilot-central", {"X-metadata-token": token})
	payload = json.loads(attributes)
	return payload["central_endpoint"].rstrip("/"), payload["central_auth_token"]


def call(method: str, http_method: str, domain: str):
	"""Call one central.api.pilot method and return its message."""
	endpoint, token = credentials()
	url = f"{endpoint}/api/method/central.api.pilot.{method}"
	headers = {"X-Pilot-Token": token, "Accept": "application/json", "User-Agent": USER_AGENT}
	body = None
	if http_method == "GET":
		url = f"{url}?{urllib.parse.urlencode({'domain': domain})}"
	else:
		body = json.dumps({"domain": domain}).encode()
		headers["Content-Type"] = "application/json"

	request = urllib.request.Request(url, data=body, headers=headers, method=http_method)
	with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
		return json.loads(response.read() or b"{}").get("message")


def error_message(error: urllib.error.HTTPError) -> str:
	"""The first user-facing message Frappe sent, without the traceback."""
	try:
		payload = json.loads(error.read())
		messages = json.loads(payload.get("_server_messages") or "[]")
		if messages:
			return json.loads(messages[0]).get("message", "")
		return payload.get("exception", "").split(":", 1)[-1].strip()
	except ValueError:
		return f"Central returned HTTP {error.code}."


def route_policy(domain: str) -> dict:
	"""The route Pilot must use for a registered hostname."""
	name = domain.strip().lower().rstrip(".")
	for pattern in WILDCARD_DOMAINS:
		suffix = pattern.strip().lower().removeprefix("*").rstrip(".")
		if name != suffix.lstrip(".") and name.endswith(suffix):
			return SITE_ROUTE
	return DOMAIN_ROUTE


def run(arguments: argparse.Namespace) -> int:
	if arguments.command == "generate-dns-records":
		print(json.dumps(call("domain_records", "GET", arguments.domain) or {}))
	elif arguments.command == "register":
		call("register_domain", "POST", arguments.domain)
		print(json.dumps(route_policy(arguments.domain)))
	elif arguments.command == "deregister":
		try:
			call("deregister_domain", "POST", arguments.domain)
		except (OSError, ValueError, KeyError, TypeError) as error:
			print(f"Warning: could not release {arguments.domain}: {error}", file=sys.stderr)
	elif arguments.command == "proxy-servers":
		print(json.dumps(PROXY_SERVERS))
	elif arguments.command == "wildcard-domains":
		print(json.dumps(WILDCARD_DOMAINS))
	return 0


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	commands = parser.add_subparsers(dest="command", required=True)

	records = commands.add_parser("generate-dns-records", help="DNS records a domain needs")
	records.add_argument("site")
	records.add_argument("domain")
	commands.add_parser("register", help="claim a domain").add_argument("domain")
	commands.add_parser("deregister", help="release a domain").add_argument("domain")
	commands.add_parser("proxy-servers", help="edge proxy addresses Pilot must trust")
	commands.add_parser("wildcard-domains", help="wildcard domains the edge serves")

	arguments = parser.parse_args()
	try:
		return run(arguments)
	except urllib.error.HTTPError as error:
		print(error_message(error) or f"Central returned HTTP {error.code}.", file=sys.stderr)
		# Pilot reads 2 as a refusal.
		return 2 if error.code in (403, 409) else 1
	except (OSError, ValueError, KeyError) as error:
		print(f"Domain provider is not available: {error}", file=sys.stderr)
		return 1


if __name__ == "__main__":
	sys.exit(main())
