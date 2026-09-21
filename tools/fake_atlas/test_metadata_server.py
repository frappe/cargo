import http.client
import threading
import unittest
from http.server import ThreadingHTTPServer

from tools.fake_atlas.metadata_server import MetadataState, make_handler


class TestMetadataServer(unittest.TestCase):
	def setUp(self):
		self.now = 100.0
		state = MetadataState(
			{"pilot-central": '{"central_endpoint":"https://central.test"}'},
			clock=lambda: self.now,
		)
		self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
		self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
		self.thread.start()

	def tearDown(self):
		self.server.shutdown()
		self.server.server_close()
		self.thread.join()

	def request(self, method: str, path: str, headers: dict[str, str] | None = None):
		connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
		self.addCleanup(connection.close)
		connection.request(method, path, headers=headers or {})
		response = connection.getresponse()
		return response.status, response.read().decode()

	def issue_token(self, ttl: str = "60") -> str:
		status, token = self.request(
			"PUT",
			"/latest/api/token",
			{"X-metadata-token-ttl-seconds": ttl},
		)
		self.assertEqual(status, 200)
		self.assertTrue(token)
		return token

	def test_token_ttl_is_required(self):
		status, _ = self.request("PUT", "/latest/api/token")
		self.assertEqual(status, 400)

	def test_token_ttl_must_be_in_the_supported_range(self):
		for ttl in ("0", "21601", "not-a-number"):
			with self.subTest(ttl=ttl):
				status, _ = self.request(
					"PUT",
					"/latest/api/token",
					{"X-metadata-token-ttl-seconds": ttl},
				)
				self.assertEqual(status, 400)

	def test_token_is_required_to_read_an_attribute(self):
		status, _ = self.request("GET", "/latest/meta-data/attributes/pilot-central")
		self.assertEqual(status, 401)

	def test_issued_token_reads_the_exact_attribute(self):
		token = self.issue_token()
		status, body = self.request(
			"GET",
			"/latest/meta-data/attributes/pilot-central",
			{"X-metadata-token": token},
		)
		self.assertEqual((status, body), (200, '{"central_endpoint":"https://central.test"}'))

	def test_unknown_attribute_returns_not_found(self):
		token = self.issue_token()
		status, _ = self.request(
			"GET",
			"/latest/meta-data/attributes/missing",
			{"X-metadata-token": token},
		)
		self.assertEqual(status, 404)

	def test_expired_token_is_rejected(self):
		token = self.issue_token("1")
		self.now += 2
		status, _ = self.request(
			"GET",
			"/latest/meta-data/attributes/pilot-central",
			{"X-metadata-token": token},
		)
		self.assertEqual(status, 401)

	def test_unimplemented_route_returns_not_found(self):
		status, _ = self.request("GET", "/latest/meta-data")
		self.assertEqual(status, 404)


if __name__ == "__main__":
	unittest.main()
