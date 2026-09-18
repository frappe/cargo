#!/usr/bin/env python3
"""A stand-in for the regional Proxy that remembers routes instead of serving them.

Speaks the shape both planes expect -- /v1/sites and /v1/domains under a Bearer token, an
address in the body -- so nothing in Cargo or Central changes. Point Cargo Settings' Proxy
URL at this and build a cluster.

    python3 fake_proxy.py --port 8200

It routes no traffic. What it is for is the assertion nothing else makes: that a real build
produces the domains ProxyClient will accept, and maps every one of them.
"""

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "/v1"
# Neither client sends a name with a slash in it, so one path segment is the whole name.
SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

ROUTES: dict[str, dict[str, str]] = {"sites": {}, "domains": {}}
LOCK = threading.Lock()
# Requests answered 503 before any are answered properly, so ProxyClient's retry is exercised.
FLAKY = {"sites": 0, "domains": 0}


class Handler(BaseHTTPRequestHandler):
	def log_message(self, *args) -> None:
		pass

	def reply(self, payload, status: int = 200) -> None:
		body = b"" if payload is None else json.dumps(payload).encode()
		self.send_response(status)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def fail(self, message: str, status: int) -> None:
		self.reply({"error": message}, status)

	@property
	def route(self) -> list[str]:
		return self.path[len(PREFIX) :].strip("/").split("/")

	def authenticated(self) -> bool:
		"""Any token passes, as in fake_atlas. A missing one does not: a client that forgot
		the header should fail here rather than in production."""
		if not self.path.startswith(PREFIX):
			self.fail("unknown endpoint", 404)
			return False
		if not (self.headers.get("Authorization") or "").startswith("Bearer "):
			self.fail("Authorization: Bearer <token> required", 401)
			return False

		return True

	def body(self) -> dict:
		length = int(self.headers.get("Content-Length") or 0)
		return json.loads(self.rfile.read(length) or "{}")

	def collection(self) -> tuple[str, str] | None:
		"""The collection and name this path addresses, or None once refused."""
		route = self.route
		if len(route) != 2 or route[0] not in ROUTES or not SEGMENT.match(route[1]):
			self.fail(f"unimplemented: {self.command} {self.path}", 404)
			return None

		return route[0], route[1]

	def flaked(self, kind: str) -> bool:
		"""Spend one of the failures asked for, so a retrying client gets past them."""
		with LOCK:
			if not FLAKY[kind]:
				return False
			FLAKY[kind] -= 1

		print(f"  ~ {kind}: answering 503 ({FLAKY[kind]} left)", flush=True)
		self.fail("the proxy is briefly unavailable", 503)

		return True

	def do_PATCH(self) -> None:
		if not self.authenticated():
			return

		addressed = self.collection()
		if not addressed:
			return

		kind, name = addressed
		if self.flaked(kind):
			return

		address = (self.body().get("address") or "").strip()
		if not address:
			self.fail("the request needs an address", 400)
			return

		with LOCK:
			ROUTES[kind][name] = address
		print(f"  + {kind[:-1]} {name} -> {address}", flush=True)
		self.reply({"name": name, "address": address})

	def do_DELETE(self) -> None:
		if not self.authenticated():
			return

		addressed = self.collection()
		if not addressed:
			return

		kind, name = addressed
		with LOCK:
			removed = ROUTES[kind].pop(name, None)
		# The real proxy succeeds on a route that is already gone, so deletion can be retried.
		print(f"  - {kind[:-1]} {name}{'' if removed else ' (was not mapped)'}", flush=True)
		self.reply(None, 204)

	def do_GET(self) -> None:
		"""Read back what was mapped. The real proxy has no such route: this exists so a test
		can assert the mapping happened."""
		if not self.authenticated():
			return

		kind = self.route[0]
		if self.route[1:] or kind not in ROUTES:
			self.fail(f"unimplemented: GET {self.path}", 404)
			return

		with LOCK:
			self.reply({"items": [{"name": name, "address": address} for name, address in ROUTES[kind].items()]})


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--port", type=int, default=8200)
	parser.add_argument(
		"--flaky",
		type=int,
		default=0,
		metavar="N",
		help="answer the first N writes to each collection with 503, so the client's retry is exercised",
	)
	args = parser.parse_args()

	FLAKY["sites"] = FLAKY["domains"] = args.flaky
	print(f"fake proxy on http://127.0.0.1:{args.port}  (flaky={args.flaky})")
	print("point Cargo Settings' Proxy URL at it, then build a cluster")
	print(f"read back what was mapped: curl -H 'Authorization: Bearer x' http://127.0.0.1:{args.port}/v1/sites\n")
	ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
	main()
