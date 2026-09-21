import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from tools.fake_atlas.fake_atlas import Handler


class TestFakeAtlas(unittest.TestCase):
	def setUp(self):
		self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
		self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
		self.thread.start()

	def tearDown(self):
		self.server.shutdown()
		self.server.server_close()
		self.thread.join()

	def test_tagged_system_image_request_returns_the_base_image(self):
		connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
		self.addCleanup(connection.close)
		connection.request(
			"GET",
			"/api/atlas/images?image_type=system&tag=purpose:base,os:Ubuntu,os_version:24.04&limit=100",
			headers={"Authorization": "Bearer test", "X-Tenant-ID": "1"},
		)

		response = connection.getresponse()
		payload = json.load(response)

		self.assertEqual(response.status, 200)
		self.assertEqual(payload["items"][0]["id"], "ubuntu-24.04")


if __name__ == "__main__":
	unittest.main()
