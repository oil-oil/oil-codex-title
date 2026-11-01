"""显式启用的真实 CLI 验收；仅连接本地合成 Responses 接口，不调用外部模型。"""
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import oil_codex_title as app
from codex_adapter import generate_json, SCHEMA
import archive_policy as archive

BINARY = os.environ.get("OIL_CODEX_TITLE_TEST_CODEX")


@unittest.skipUnless(BINARY, "设置 OIL_CODEX_TITLE_TEST_CODEX 才运行真实 CLI 本地验收")
class RelayCLITests(unittest.TestCase):
    def test_real_cli_posts_to_relay_for_naming_and_archive(self):
        requests = []
        candidates = [{"action": "rename", "title": "🧩 接口配置｜修复", "reason": "合成结果"},
                      {"classification": "completed", "reason": "合成结果"}]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                text = json.dumps(candidates[len(requests) - 1], ensure_ascii=False)
                item = {"id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}]}
                response = {"id": "resp_test", "object": "response", "created_at": 1, "status": "completed",
                            "model": "relay-test-model", "output": [item],
                            "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20,
                                      "input_tokens_details": {"cached_tokens": 0},
                                      "output_tokens_details": {"reasoning_tokens": 0}}}
                events = [
                    {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                    {"type": "response.output_item.added", "output_index": 0,
                     "item": {**item, "status": "in_progress", "content": []}},
                    {"type": "response.content_part.added", "item_id": "msg_test", "output_index": 0,
                     "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}},
                    {"type": "response.output_text.delta", "item_id": "msg_test", "output_index": 0,
                     "content_index": 0, "delta": text},
                    {"type": "response.output_text.done", "item_id": "msg_test", "output_index": 0,
                     "content_index": 0, "text": text},
                    {"type": "response.content_part.done", "item_id": "msg_test", "output_index": 0,
                     "content_index": 0, "part": item["content"][0]},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": response},
                ]
                payload = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False)
                                  + "\n\n" for event in events).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            real_run = subprocess.run
            def checked_run(*args, **kwargs):
                proc = real_run(*args, **kwargs)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                return proc
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "CODEX_HOME": tmp, "TITLE_LOCAL_TEST_KEY": "synthetic-local-key",
                "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
            }), patch("codex_adapter.subprocess.run", side_effect=checked_run):
                config = {**app.DEFAULTS, "model_timeout_seconds": 20, "provider": "relay", "relay": {
                    "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    "api_key_env": "TITLE_LOCAL_TEST_KEY", "model": "relay-test-model"}}
                for index, (policy, schema) in enumerate(((ROOT / "prompts/naming.md", SCHEMA),
                                                          (ROOT / "prompts/archiving.md", archive.SCHEMA))):
                    result, _ = generate_json(BINARY, config, {"original_goal": "修复接口配置"}, policy, schema)
                    self.assertEqual(result, candidates[index])
            self.assertEqual(len(requests), 2)
            for request in requests:
                self.assertEqual(request["path"], "/v1/responses")
                self.assertEqual(request["auth"], "Bearer synthetic-local-key")
                self.assertEqual(request["body"]["model"], "relay-test-model")
                self.assertNotIn("service_tier", request["body"])
                # 部分 CLI 始终提供只在 Plan 模式可用的输入工具；命名运行于默认模式。
                self.assertLessEqual({tool.get("name") for tool in request["body"].get("tools", [])},
                                     {"request_user_input"})
                self.assertEqual(request["body"]["text"]["format"]["type"], "json_schema")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
