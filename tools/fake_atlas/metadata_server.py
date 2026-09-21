#!/usr/bin/env python3
import argparse
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

MAX_TOKEN_TTL_SECONDS = 21600
TOKEN_PATH = "/latest/api/token"
ATTRIBUTE_PREFIX = "/latest/meta-data/attributes/"


@dataclass
class MetadataState:
	attributes: dict[str, str]
	clock: Callable[[], float] = time.monotonic
	tokens: dict[str, float] = field(default_factory=dict)

	def issue_token(self, ttl: int) -> str:
		token = secrets.token_urlsafe(32)
		self.tokens[token] = self.clock() + ttl
		return token

	def accepts(self, token: str) -> bool:
		expires_at = self.tokens.get(token)
		if expires_at is None or expires_at <= self.clock():
			self.tokens.pop(token, None)
			return False
		return True


def make_handler(state: MetadataState) -> type[BaseHTTPRequestHandler]:
	class MetadataHandler(BaseHTTPRequestHandler):
		def log_message(self, *args) -> None:
			pass

		def reply(self, status: int, body: str = "") -> None:
			encoded = body.encode()
			self.send_response(status)
			self.send_header("Content-Type", "text/plain; charset=utf-8")
			self.send_header("Content-Length", str(len(encoded)))
			self.end_headers()
			self.wfile.write(encoded)

		def do_PUT(self) -> None:
			if urlsplit(self.path).path != TOKEN_PATH:
				self.reply(404)
				return

			try:
				ttl = int(self.headers.get("X-metadata-token-ttl-seconds", ""))
			except ValueError:
				self.reply(400)
				return
			if not 1 <= ttl <= MAX_TOKEN_TTL_SECONDS:
				self.reply(400)
				return
			self.reply(200, state.issue_token(ttl))

		def do_GET(self) -> None:
			path = urlsplit(self.path).path
			if not path.startswith(ATTRIBUTE_PREFIX):
				self.reply(404)
				return

			token = self.headers.get("X-metadata-token", "")
			if not state.accepts(token):
				self.reply(401)
				return

			name = unquote(path.removeprefix(ATTRIBUTE_PREFIX))
			value = state.attributes.get(name)
			if value is None:
				self.reply(404)
				return
			self.reply(200, value)

	return MetadataHandler


def serve(metadata_path: str, host: str = "169.254.169.254", port: int = 80) -> None:
	attributes = json.loads(Path(metadata_path).read_text())
	if not isinstance(attributes, dict) or not all(
		isinstance(key, str) and isinstance(value, str) for key, value in attributes.items()
	):
		raise ValueError("metadata must be an object of string attributes")
	ThreadingHTTPServer((host, port), make_handler(MetadataState(attributes))).serve_forever()


def main() -> None:
	parser = argparse.ArgumentParser(description="Serve Fake Atlas instance metadata.")
	parser.add_argument("metadata_path")
	args = parser.parse_args()
	serve(args.metadata_path)


if __name__ == "__main__":
	main()
