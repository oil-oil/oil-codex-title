"""官方默认与中转站配置回归；普通测试只使用合成配置和模拟进程。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import oil_codex_title as app
from codex_adapter import BackendError, generate_json, SCHEMA
from model_config import effective_model_config
from usage_ledger import usage_scope

RELAY = {"base_url": "https://relay.example/v1", "api_key_env": "TITLE_TEST_KEY",
         "model": "relay-model", "service_tier": None}
CANDIDATE = {"action": "rename", "title": "🧩 接口配置｜修复", "reason": "目标明确"}


class ModelConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = {**app.DEFAULTS, "provider": "relay", "relay": RELAY.copy()}

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        proc = subprocess.run([sys.executable, str(ROOT / "scripts/oil_codex_title.py"),
                               "configure", *args], capture_output=True, encoding="utf-8",
                              env=os.environ | {"OIL_CODEX_TITLE_DATA": str(self.root)}, timeout=10)
        return proc.returncode, json.loads(proc.stdout)

    def fake_run(self, args, **kwargs):
        self.args, self.kwargs = args, kwargs
        Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(CANDIDATE), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def run_model(self, config=None):
        with patch.dict(os.environ, {"TITLE_TEST_KEY": "synthetic-secret"}), \
             patch("codex_adapter.subprocess.run", side_effect=self.fake_run):
            return generate_json("unused", config or self.config, {}, ROOT / "prompts/naming.md", SCHEMA)

    def overrides(self):
        return [self.args[i + 1] for i, arg in enumerate(self.args) if arg == "-c"]

    def test_legacy_config_remains_official_luna_fast(self):
        app.atomic_json(self.root / "config.json", {"future": 1})
        config = app.load_config(self.root)
        self.assertEqual((config["provider"], config["model"], config["service_tier"]),
                         ("official", "gpt-5.6-luna", "priority"))
        self.assertEqual(config["future"], 1)
        self.run_model(config)
        self.assertIn("--ignore-user-config", self.args)
        self.assertIn('service_tier="priority"', self.overrides())
        self.assertFalse(any(c.startswith("model_provider") for c in self.overrides()))
        self.assertNotIn("skills.max_context_tokens=1", self.overrides())

    def test_relay_launch_uses_env_auth_and_preserves_isolation(self):
        self.run_model()
        self.assertEqual(self.args[self.args.index("-m") + 1], "relay-model")
        self.assertIn('model_provider="oil_title_relay"', self.overrides())
        provider, = [c for c in self.overrides() if c.startswith("model_providers.")]
        self.assertIn('base_url = "https://relay.example/v1"', provider)
        self.assertIn('env_key = "TITLE_TEST_KEY"', provider)
        self.assertIn('wire_api = "responses"', provider)
        self.assertIn('requires_openai_auth = false', provider)
        self.assertIn('supports_websockets = false', provider)
        self.assertNotIn("synthetic-secret", " ".join(self.args))
        self.assertEqual(self.kwargs["env"]["TITLE_TEST_KEY"], "synthetic-secret")
        self.assertNotIn("CODEX_THREAD_ID", self.kwargs["env"])
        self.assertIn("--ignore-user-config", self.args)
        self.assertIn("--ephemeral", self.args)
        for feature in ("hooks", "shell_tool", "plugins", "apps", "multi_agent"):
            self.assertIn(["--disable", feature], [self.args[i:i + 2] for i in range(len(self.args) - 1)])
        self.assertFalse(any(c.startswith("service_tier=") for c in self.overrides()))
        self.assertIn("features.goals=false", self.overrides())
        self.assertIn("features.view_image=false", self.overrides())
        self.assertIn("tools.view_image=false", self.overrides())

    def test_configure_switch_preserves_each_provider_settings_and_unknown_fields(self):
        app.atomic_json(self.root / "config.json", {"future": 42})
        code, config = self.cli("--provider", "relay", "--base-url", RELAY["base_url"],
                                "--api-key-env", RELAY["api_key_env"], "--model", "relay-model")
        self.assertEqual(code, 0, config)
        self.assertEqual(config["relay"], RELAY)
        self.assertEqual((config["model"], config["service_tier"]), ("gpt-5.6-luna", "priority"))
        self.assertEqual(config["future"], 42)
        code, official = self.cli("--provider", "official")
        self.assertEqual(code, 0, official)
        self.assertEqual(effective_model_config(official)["model"], "gpt-5.6-luna")
        code, relay = self.cli("--provider", "relay")
        self.assertEqual(code, 0, relay)
        self.assertEqual(effective_model_config(relay)["model"], "relay-model")

    def test_relay_model_switch_clears_fast_without_changing_official(self):
        app.atomic_json(self.root / "config.json", self.config)
        code, config = self.cli("--service-tier", "fast")
        self.assertEqual(code, 0, config)
        self.assertEqual(config["relay"]["service_tier"], "priority")
        self.run_model(config)
        self.assertIn('service_tier="priority"', self.overrides())
        code, config = self.cli("--model", "another-relay-model")
        self.assertEqual(code, 0, config)
        self.assertIsNone(config["relay"]["service_tier"])
        self.assertEqual(config["service_tier"], "priority")

    def test_invalid_configuration_never_overwrites_previous_file(self):
        app.atomic_json(self.root / "config.json", {"future": 7})
        before = (self.root / "config.json").read_bytes()
        for args in (("--provider", "relay"), ("--base-url", RELAY["base_url"]), ("--model", ""),
                     ("--provider", "relay", "--base-url", "https://user:password@relay.example/v1",
                      "--api-key-env", "TITLE_TEST_KEY", "--model", "relay-model")):
            with self.subTest(args=args):
                code, _ = self.cli(*args)
                self.assertEqual(code, 1)
                self.assertEqual((self.root / "config.json").read_bytes(), before)

    def test_missing_key_fails_before_process_or_usage_attempt(self):
        with patch.dict(os.environ, {}, clear=True), patch("codex_adapter.subprocess.run") as run:
            with usage_scope(self.root, "naming"), self.assertRaisesRegex(BackendError, "密钥环境变量未设置"):
                generate_json("unused", self.config, {}, ROOT / "prompts/naming.md", SCHEMA)
        run.assert_not_called()
        self.assertFalse(list((self.root / "usage").glob("*/*.json")))

    def test_invalid_relay_values_are_rejected_without_echoing_credentials(self):
        for key, values in {"base_url": ["", "file:///tmp/x", "https://relay.example/v1?key=secret",
                                         "https://relay.example/#secret", "https://relay.example:bad", "https://relay.example:0",
                                         "https://relay.example/\n"],
                            "api_key_env": ["", "sk-secret", "TITLE TEST KEY"],
                            "model": ["", " relay-model", "relay\nmodel"],
                            "service_tier": ["auto", True]}.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError) as error:
                    effective_model_config({**self.config, "relay": {**RELAY, key: value}})
                self.assertNotIn("secret", str(error.exception))
        with self.assertRaises(ValueError):
            effective_model_config({**self.config, "relay": {**RELAY, "api_key": "synthetic-secret"}})

    def test_usage_records_actual_relay_model_and_tier_without_credentials(self):
        with usage_scope(self.root, "naming"):
            self.run_model()
        row, = list((self.root / "usage").glob("*/*.json"))
        record = json.loads(row.read_text(encoding="utf-8"))
        self.assertEqual(record["model"], "relay-model")
        self.assertIsNone(record["service_tier"])
        self.assertNotIn("synthetic-secret", row.read_text())
        self.assertNotIn("relay.example", row.read_text())

    def test_relay_timeout_and_failure_do_not_fallback_to_official(self):
        for failure in (subprocess.TimeoutExpired("unused", 1), SimpleNamespace(returncode=1, stdout="", stderr="secret")):
            with self.subTest(failure=type(failure).__name__), patch.dict(os.environ, {"TITLE_TEST_KEY": "synthetic-secret"}), \
                 patch("codex_adapter.subprocess.run", side_effect=failure if isinstance(failure, Exception) else None,
                       return_value=failure) as run:
                with self.assertRaises(BackendError) as error:
                    generate_json("unused", self.config, {}, ROOT / "prompts/naming.md", SCHEMA)
                self.assertEqual(run.call_count, 1)
                self.assertNotIn("secret", str(error.exception))

    def test_doctor_reports_selected_model_and_key_presence_without_secrets(self):
        for value, present in (("synthetic-secret", True), ("", False)):
            with self.subTest(present=present), patch.dict(os.environ, {"TITLE_TEST_KEY": value}), \
                 patch.object(app, "subprocess") as processes, patch.object(app, "CodexBackend") as backend:
                processes.run.return_value.stdout = "codex-test"
                backend.return_value.__enter__.return_value = MagicMock()
                report = app.doctor("unused", self.root, self.config)
                self.assertEqual(report["model_connection"], {"provider": "relay", "model": "relay-model",
                    "inference": "not_verified", "api_key_env": "TITLE_TEST_KEY", "api_key_present": present})
                self.assertNotIn("synthetic-secret", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
