"""本地设置页 HTTP、真实配置写入和外部并发修改回归；不调用模型。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import oil_codex_title as app
from settings_server import create_server

RELAY = {"base_url": "https://relay.example/v1", "api_key_env": "TITLE_SETTINGS_TEST_KEY",
         "model": "relay-test-model", "service_tier": "standard"}


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.key_writer = Mock()
        self.server = create_server(self.root, key_writer=self.key_writer)
        self.url = self.server.settings_url.split("#")[0]
        self.token = urlsplit(self.server.settings_url).fragment.split("=", 1)[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.http = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, path="/api/config", *, method="GET", data=None, headers=None, auth=True):
        fields = {"X-Oil-Settings-Token": self.token} if auth else {}
        if data is not None:
            fields["Content-Type"] = "application/json"
        fields.update(headers or {})
        body = json.dumps(data).encode("utf-8") if data is not None else None
        try:
            response = self.http.open(Request(self.url.rstrip("/") + path, data=body, headers=fields, method=method), timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read()
            value = json.loads(raw) if "application/json" in response.headers["Content-Type"] else raw.decode("utf-8")
            return response.status, value, response.headers

    def save(self, provider="relay", **changes):
        _, config, _ = self.request()
        payload = {"revision": config["revision"], "provider": provider,
                   **(RELAY if provider == "relay" else config["official"]), **changes}
        return self.request(method="PUT", data=payload)

    def test_static_page_and_assets_only_serve_allowlisted_paths(self):
        code, page, _ = self.request("/", auth=False)
        self.assertEqual(code, 200)
        self.assertIn("<title>mogoo-codex-title · 设置</title>", page)
        self.assertIn(">mogoo-codex-title</span>", page)
        for path in ("/", "/settings.js", "/settings.css"):
            code, value, headers = self.request(path, auth=False)
            self.assertEqual(code, 200)
            self.assertGreater(len(value), 100)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertNotIn(self.token, value)
        self.assertEqual(self.request("/../../config.json")[0], 404)

    def test_settings_read_preserves_default_without_creating_config(self):
        code, value, _ = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(value["provider"], "official")
        self.assertEqual(value["official"], {"model": "gpt-5.6-luna", "service_tier": "fast"})
        self.assertIsNone(value["relay"])
        self.assertFalse((self.root / "config.json").exists())

    def test_page_save_and_switch_preserve_each_mode_and_unknown_config(self):
        app.atomic_json(self.root / "config.json", {"future": 7, "enabled": False, "codex_bin": "synthetic-path"})
        code, value, _ = self.save()
        self.assertEqual(code, 200, value)
        self.assertEqual(value["provider"], "relay")
        self.assertEqual(value["relay"], RELAY)
        code, value, _ = self.save("official")
        self.assertEqual(code, 200, value)
        self.assertEqual(value["provider"], "official")
        self.assertEqual(value["relay"], RELAY)
        stored = app.load_config(self.root)
        self.assertEqual(stored["future"], 7)
        self.assertFalse(stored["enabled"])
        self.assertEqual(stored["codex_bin"], "synthetic-path")
        self.assertNotIn("future", value)
        self.assertNotIn("codex_bin", value)

    def test_invalid_save_and_key_value_never_overwrite_config(self):
        app.atomic_json(self.root / "config.json", {"future": 8})
        before = (self.root / "config.json").read_bytes()
        for change in ({"model": ""}, {"base_url": "https://relay.example/v1?key=synthetic-secret"},
                       {"api_key_env": "sk-synthetic-secret"}, {"api_key": "synthetic-secret"},
                       {"service_tier": "auto"}):
            with self.subTest(change=change):
                code, value, _ = self.save(**change)
                self.assertEqual(code, 400, value)
                self.assertNotIn("synthetic-secret", json.dumps(value))
                self.assertEqual((self.root / "config.json").read_bytes(), before)

    def test_changed_config_returns_conflict_and_preserves_external_edit(self):
        _, view, _ = self.request()
        app.atomic_json(self.root / "config.json", {"model": "external-model", "future": 42})
        code, _, _ = self.request(method="PUT", data={"revision": view["revision"], "provider": "relay", **RELAY})
        self.assertEqual(code, 409)
        self.assertEqual(app.load_config(self.root)["model"], "external-model")

    def test_authentication_origin_and_host_required_for_config(self):
        for fields, auth in (({}, False), ({"X-Oil-Settings-Token": "wrong"}, True),
                             ({"Origin": "https://external.example"}, True),
                             ({"Host": "external.example"}, True)):
            with self.subTest(fields=fields):
                self.assertEqual(self.request(headers=fields, auth=auth)[0], 403)
                self.assertEqual(self.request(method="PUT", data={}, headers=fields, auth=auth)[0], 403)
        self.assertEqual(self.request(headers={"Origin": self.url.rstrip("/")})[0], 200)

    def test_key_status_only_returns_presence_without_secret_or_unknown_config(self):
        self.save()
        with patch.dict(os.environ, {"TITLE_SETTINGS_TEST_KEY": "synthetic-secret"}):
            code, view, _ = self.request()
        self.assertEqual(code, 200)
        self.assertTrue(view["api_key_present"])
        self.assertNotIn("synthetic-secret", json.dumps(view))

    def test_cli_model_change_visible_to_page_and_can_be_saved_after_reload(self):
        from types import SimpleNamespace
        app.update_config(self.root, "configure", SimpleNamespace(provider=None, model="new-official-model",
            service_tier=None, base_url=None, api_key_env=None, codex_bin=None))
        _, view, _ = self.request()
        self.assertEqual(view["official"], {"model": "new-official-model", "service_tier": "standard"})
        self.assertEqual(self.save()[0], 200)

    def test_time_switch_is_global_and_retained_when_switching_provider(self):
        self.assertTrue(self.request()[1]['show_last_user_time'])
        code, view, _ = self.save(show_last_user_time=False)
        self.assertEqual(code, 200, view)
        self.assertFalse(view['show_last_user_time'])
        self.assertFalse(self.save('official')[1]['show_last_user_time'])
        code, view, _ = self.save(show_last_user_time='off')
        self.assertEqual(code, 400)
        self.assertFalse(app.load_config(self.root)['show_last_user_time'])

    def test_key_save_only_calls_user_environment_writer_without_config_write_or_echo(self):
        code, result, _ = self.request("/api/key", method="PUT", data={
            "api_key_env": "OIL_TITLE_RELAY_KEY", "api_key": "synthetic-test-key"})
        self.assertEqual(code, 200, result)
        self.key_writer.assert_called_once_with("OIL_TITLE_RELAY_KEY", "synthetic-test-key")
        self.assertEqual(result, {"status": "key_saved", "api_key_env": "OIL_TITLE_RELAY_KEY",
                                 "api_key_present": True, "restart_required": True})
        self.assertNotIn("synthetic-test-key", json.dumps(result))
        self.assertFalse((self.root / "config.json").exists())

    def test_invalid_key_or_reserved_environment_never_reaches_writer(self):
        for payload in ({"api_key_env": "PATH", "api_key": "synthetic-key"},
                        {"api_key_env": "CODEX_HOME", "api_key": "synthetic-key"},
                        {"api_key_env": "bad-name", "api_key": "synthetic-key"},
                        {"api_key_env": "TITLE_KEY", "api_key": ""},
                        {"api_key_env": "TITLE_KEY", "api_key": "synthetic-key\n"},
                        {"api_key_env": "TITLE_KEY", "api_key": "synthetic-key", "extra": 1}):
            with self.subTest(payload=payload):
                code, value, _ = self.request("/api/key", method="PUT", data=payload)
                self.assertEqual(code, 400, value)
                self.assertNotIn("synthetic-key", json.dumps(value))
        self.key_writer.assert_not_called()

    def test_key_write_failure_and_unauthorized_calls_do_not_expose_secret(self):
        self.key_writer.side_effect = OSError("synthetic-test-key")
        payload = {"api_key_env": "TITLE_KEY", "api_key": "synthetic-test-key"}
        code, result, _ = self.request("/api/key", method="PUT", data=payload)
        self.assertEqual(code, 500)
        self.assertNotIn("synthetic-test-key", json.dumps(result))
        self.key_writer.reset_mock()
        for headers, auth in (({}, False), ({"Origin": "https://outside.example"}, True),
                              ({"Host": "outside.example"}, True)):
            self.assertEqual(self.request("/api/key", method="PUT", data=payload, headers=headers, auth=auth)[0], 403)
        self.key_writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
