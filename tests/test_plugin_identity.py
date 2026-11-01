"""插件改名的打包、入口和缓存迁移回归；仅用合成目录，不调用模型。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import oil_codex_title as app
from worker_setup import RUNNER

NAME = "mogoo-codex-title"


class PluginIdentityTests(unittest.TestCase):
    def test_plugin_marketplace_skill_and_hook_resolve_the_same_package(self):
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], NAME)
        self.assertEqual(manifest["interface"]["displayName"], NAME)
        self.assertIn(NAME, manifest["interface"]["defaultPrompt"])
        self.assertEqual(marketplace["name"], NAME)
        self.assertEqual([item["name"] for item in marketplace["plugins"]], [NAME])
        package = ROOT / marketplace["plugins"][0]["source"]["path"]
        self.assertEqual(package.resolve(), ROOT)
        skill_dir = package / manifest["skills"] / NAME
        skill = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("\nname: " + NAME + "\n", skill)
        self.assertIn("mogoo_codex_title.py", (skill_dir / "scripts/run.py").read_text(encoding="utf-8"))
        hooks = json.loads((package / "hooks/hooks.json").read_text(encoding="utf-8"))
        command = hooks["hooks"]["Stop"][0]["hooks"][0]
        for field in ("command", "commandWindows"):
            self.assertIn("/scripts/mogoo_codex_title.py", command[field])
        self.assertTrue((package / "scripts/mogoo_codex_title.py").is_file())

    def test_install_instructions_match_identity_and_keep_real_repository_links(self):
        for filename in ("README.md", "docs/使用与边界.md"):
            with self.subTest(filename=filename):
                text = (ROOT / filename).read_text(encoding="utf-8")
                self.assertIn("codex plugin add " + NAME + "@" + NAME, text)
                self.assertIn("codex plugin marketplace upgrade " + NAME, text)
                self.assertIn("marketplace add MogooStudio/oil-codex-title --ref main", text)
                self.assertIn("worker-setup", text)

    def test_new_legacy_and_skill_entries_share_config_and_protected_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"enabled": False, "show_last_user_time": False, "future": "preserved"}
            app.atomic_json(root / "config.json", config)
            record = root / "threads/12345678-1234-1234-1234-123456789012.json"
            app.atomic_json(record, {"locked": True, "last_seen_title": "🧩 合成对象｜保持保护"})
            before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*.json")}
            outputs = []
            for entry in ("scripts/mogoo_codex_title.py", "scripts/oil_codex_title.py",
                          "skills/mogoo-codex-title/scripts/run.py"):
                proc = subprocess.run([sys.executable, str(ROOT / entry), "status"],
                                      env=os.environ | {"OIL_CODEX_TITLE_DATA": tmp},
                                      capture_output=True, encoding="utf-8", timeout=10)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                outputs.append(json.loads(proc.stdout))
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(outputs[0], outputs[2])
            self.assertFalse(outputs[0]["config"]["enabled"])
            self.assertEqual(outputs[0]["tracked_threads"], 1)
            self.assertEqual(before, {path.relative_to(root): path.read_bytes() for path in root.rglob("*.json")})

    def test_default_data_directory_keeps_legacy_location(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CODEX_HOME": tmp}):
            os.environ.pop("OIL_CODEX_TITLE_DATA", None)
            self.assertEqual(app.data_dir(), Path(tmp) / "oil-codex-title")

    def test_new_hook_entry_preserves_empty_json_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run([sys.executable, str(ROOT / "scripts/mogoo_codex_title.py"), "hook"],
                                  input="{}", env=os.environ | {"OIL_CODEX_TITLE_DATA": tmp,
                                                               "OIL_CODEX_TITLE_WORKER": "1"},
                                  capture_output=True, encoding="utf-8", timeout=10)
            self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "{}\n", ""))

    def test_doctor_does_not_mistake_trusted_legacy_hook_for_new_plugin(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, "subprocess") as processes, \
             patch.object(app, "CodexBackend") as backend, patch.object(app, "owner_process", return_value=True):
            processes.run.return_value.stdout = "codex-test"
            host = MagicMock()
            backend.return_value.__enter__.return_value = host
            old = {"pluginId": "oil-codex-title@mogoo-oil-title", "enabled": True, "trustStatus": "trusted"}
            new = {"pluginId": NAME + "@" + NAME, "enabled": True, "trustStatus": "untrusted"}
            host.call.return_value = {"data": [{"hooks": [old, new]}]}
            self.assertEqual(app.doctor("unused", Path(tmp), app.DEFAULTS)["hook"]["status"], "needs_trust_or_enable")
            new["trustStatus"] = "trusted"
            report = app.doctor("unused", Path(tmp), app.DEFAULTS)["hook"]
            self.assertEqual(report["status"], "ready")
            self.assertEqual(len(report["definitions"]), 1)
            host.call.return_value = {"data": [{"hooks": [old]}]}
            self.assertEqual(app.doctor("unused", Path(tmp), app.DEFAULTS)["hook"]["status"], "not_loaded")


class RunnerMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def package(self, marketplace, name, version, *, entry=True):
        package = self.home / "plugins/cache" / marketplace / name / version
        app.atomic_json(package / ".codex-plugin/plugin.json", {"name": name, "version": version})
        scripts = package / "scripts"
        scripts.mkdir()
        (scripts / "handoff.py").touch()
        path = scripts / ("mogoo_codex_title.py" if name == NAME else "oil_codex_title.py")
        if entry:
            path.touch()
        return path

    def selected(self):
        code = RUNNER.format(home=repr(str(self.home)), root=repr(str(self.home / "legacy-data")))
        with patch.dict(os.environ, {}), patch("subprocess.run") as run:
            exec(compile(code, "<synthetic-worker-runner>", "exec"), {})
            if not run.called:
                return None
            run.assert_called_once()
            argv = run.call_args.args[0]
            self.assertEqual(argv[-1], "worker")
            return Path(argv[-2])

    def test_new_name_precedes_legacy_cache_even_with_higher_legacy_version(self):
        self.package("mogoo-oil-title", "oil-codex-title", "99.0.0")
        expected = self.package(NAME, NAME, "0.4.3")
        self.assertEqual(self.selected(), expected)

    def test_latest_complete_new_version_wins_and_incomplete_cache_is_skipped(self):
        self.package(NAME, NAME, "0.4.9")
        expected = self.package(NAME, NAME, "0.4.11")
        self.package(NAME, NAME, "0.5.0", entry=False)
        self.assertEqual(self.selected(), expected)

    def test_new_name_in_legacy_marketplace_is_supported(self):
        expected = self.package("mogoo-oil-title", NAME, "0.4.3")
        self.assertEqual(self.selected(), expected)

    def test_legacy_cache_remains_available_during_migration(self):
        expected = self.package("mogoo-oil-title", "oil-codex-title", "0.4.2")
        self.assertEqual(self.selected(), expected)

    def test_mismatched_metadata_and_empty_cache_never_start_worker(self):
        self.assertIsNone(self.selected())
        entry = self.package(NAME, NAME, "0.4.3")
        app.atomic_json(entry.parents[1] / ".codex-plugin/plugin.json", {"name": "unrelated", "version": "0.4.3"})
        self.assertIsNone(self.selected())


if __name__ == "__main__":
    unittest.main()
