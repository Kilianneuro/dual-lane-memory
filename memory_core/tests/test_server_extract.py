from __future__ import annotations

import http.client
import json
import threading
import unittest

from memory_core.server import create_server
from memory_core.store import MemoryStore


class ExtractEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore(":memory:", owner_id="10001")
        self.server = create_server(self.store, "127.0.0.1", 0, "api-token", "admin-token")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()

    def _post(self, path: str, payload: dict, token: str = "api-token"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request("POST", path, json.dumps(payload), {
                "Content-Type": "application/json",
                "X-Memory-Token": token,
            })
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()

    def test_extract_endpoint(self) -> None:
        status, data = self._post("/v1/memory/extract", {
            "text": "我现在在玩只狼，卡在苇名弦一郎",
            "source": {"platform": "qq", "context_type": "group", "context_id": "1001"},
        })
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["facts"][0]["category"], "game_progress")
        self.assertIn("只狼", data["facts"][0]["content"])

    def test_extract_requires_auth(self) -> None:
        status, _ = self._post("/v1/memory/extract", {"text": "我喜欢吃火锅"}, token="wrong")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
