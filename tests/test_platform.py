"""跨平台真实进程锁、Windows 入口与 UTF-8 边界测试；不调用模型。"""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import oil_codex_title as app
from codex_adapter import BackendError, windows_binary, find_codex, generate_title

ID = '12345678-1234-1234-1234-123456789012'


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='title-test-')
        self.root = Path(self.tmp.name) / '中文 空格'
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def child(self, code, *args):
        return subprocess.Popen([sys.executable, '-u', '-c',
            'import sys; sys.path.insert(0, sys.argv[1]); ' + code,
            str(ROOT / 'scripts'), *map(str, args)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')

    def test_lock_excludes_another_process_then_releases(self):
        code = ('from pathlib import Path; import oil_codex_title as t; '
                'lock=t.thread_lock(Path(sys.argv[2]), sys.argv[3]); '
                'print(int(lock.__enter__()), flush=True); lock.__exit__(None,None,None)')
        with app.thread_lock(self.root, ID):
            p = self.child(code, self.root, ID)
            output, errors = p.communicate(timeout=10)
            self.assertEqual((p.returncode, output.strip(), errors), (0, '0', ''))
        p = self.child(code, self.root, ID)
        output, errors = p.communicate(timeout=10)
        self.assertEqual((p.returncode, output.strip(), errors), (0, '1', ''))

    def test_killed_process_does_not_leave_stale_lock(self):
        p = self.child('from pathlib import Path; import oil_codex_title as t; '
            'lock=t.thread_lock(Path(sys.argv[2]),sys.argv[3]); '
            'print(int(lock.__enter__()),flush=True); sys.stdin.readline()', self.root, ID)
        try:
            self.assertEqual(p.stdout.readline().strip(), '1')
            with app.thread_lock(self.root, ID) as acquired:
                self.assertFalse(acquired)
        finally:
            p.terminate()
            p.communicate(timeout=10)
        with app.thread_lock(self.root, ID) as acquired:
            self.assertTrue(acquired)

    def make_npm_binary(self, folder, target='x86_64-pc-windows-msvc', layout='bin'):
        exe = self.root / folder / 'vendor' / target / layout / 'codex.exe'
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b'fixture')
        return exe

    def test_windows_npm_nested_dependency_is_resolved_without_shell(self):
        exe = self.make_npm_binary('node_modules/@openai/codex/node_modules/@openai/codex-win32-x64')
        with patch('codex_adapter.platform.machine', return_value='AMD64'):
            self.assertEqual(Path(windows_binary(str(self.root / 'codex.cmd'))).resolve(), exe.resolve())

    def test_windows_npm_hoisted_legacy_layout(self):
        exe = self.make_npm_binary('node_modules/@openai/codex-win32-x64', layout='codex')
        with patch('codex_adapter.platform.machine', return_value='AMD64'):
            self.assertEqual(Path(windows_binary(str(self.root / 'codex.cmd'))).resolve(), exe.resolve())

    def test_windows_npm_arm64_layout(self):
        exe = self.make_npm_binary('node_modules/@openai/codex-win32-arm64', target='aarch64-pc-windows-msvc')
        with patch('codex_adapter.platform.machine', return_value='ARM64'):
            self.assertEqual(Path(windows_binary(str(self.root / 'codex.cmd'))).resolve(), exe.resolve())

    def test_unknown_windows_shim_fails_with_actionable_message(self):
        with self.assertRaisesRegex(BackendError, 'codex-bin'):
            windows_binary(str(self.root / 'codex.cmd'))

    def test_windows_native_path_is_preferred(self):
        exe = str(self.root / 'codex.exe')
        with patch('codex_adapter.sys.platform', 'win32'), \
             patch('codex_adapter.shutil.which', side_effect=lambda name: exe if name=='codex.exe' else None), \
             patch('codex_adapter.codex_responds', return_value=True):
            self.assertEqual(find_codex(), exe)

    def test_windows_unlaunchable_app_binary_falls_back_to_npm(self):
        app_exe = str(self.root / 'WindowsApps' / 'codex.exe')
        npm_cmd = str(self.root / 'codex.cmd')
        npm_exe = self.make_npm_binary('node_modules/@openai/codex/node_modules/@openai/codex-win32-x64')
        entries = {'codex': app_exe, 'codex.cmd': npm_cmd, 'codex.exe': app_exe}
        with patch('codex_adapter.sys.platform', 'win32'), \
             patch('codex_adapter.platform.machine', return_value='AMD64'), \
             patch('codex_adapter.shutil.which', side_effect=entries.get), \
             patch('codex_adapter.codex_responds', side_effect=lambda binary: binary != app_exe):
            self.assertEqual(find_codex(), str(npm_exe))

    def test_explicit_windows_binary_does_not_silently_fall_back(self):
        app_exe = str(self.root / 'WindowsApps' / 'codex.exe')
        with patch('codex_adapter.sys.platform', 'win32'), \
             patch('codex_adapter.shutil.which', return_value=app_exe), \
             patch('codex_adapter.codex_responds', return_value=False):
            with self.assertRaisesRegex(BackendError, '配置的 Codex CLI 无法执行'):
                find_codex(app_exe)

    def test_doctor_distinguishes_tested_version_from_minimum(self):
        self.assertEqual(app.windows_cli_preflight('codex-cli 0.151.0')['version_relation'], 'older_than_tested')
        self.assertEqual(app.windows_cli_preflight('codex-cli 0.155.0')['version_relation'], 'same_or_newer')
        self.assertEqual(app.windows_cli_preflight('unexpected')['version_relation'], 'unknown')

    def test_windows_paths_cannot_leak_into_titles(self):
        for name in (r'🧩 C:\Users\example\app｜修复', r'🧩 \\server\private｜修复', '🧩 C:/Users/example/app｜修复'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                app.validate_candidate({'action':'rename','title':name,'reason':''}, '')

    def test_utf8_status_roundtrip_with_legacy_io_encoding(self):
        app.atomic_json(self.root / 'config.json', {'label':'中文 🧩｜标题'})
        env = os.environ | {'OIL_CODEX_TITLE_DATA':str(self.root), 'PYTHONIOENCODING':'ascii', 'PYTHONUTF8':'0'}
        p = subprocess.run([sys.executable,str(ROOT/'scripts/oil_codex_title.py'),'status'],
            capture_output=True, env=env, timeout=10)
        self.assertEqual(p.returncode,0,p.stderr.decode('utf-8'))
        self.assertEqual(json.loads(p.stdout.decode('utf-8'))['config']['label'],'中文 🧩｜标题')

    def test_model_process_launch_accepts_windows_flags_and_unicode_json(self):
        expected = {"action":"rename","title":"🧩 中文工具｜修复","reason":"目标明确"}
        def fake_run(args, **kwargs):
            self.assertEqual(kwargs["creationflags"], 0)
            self.assertEqual(kwargs["encoding"], "utf-8")
            self.assertEqual(json.loads(kwargs["input"])["original_goal"], "修复中文工具")
            output = Path(args[args.index("--output-last-message")+1])
            output.write_text(json.dumps(expected,ensure_ascii=False),encoding="utf-8")
            return SimpleNamespace(returncode=0,stdout="")
        with patch("codex_adapter.process_options", return_value={"creationflags":0}), patch("codex_adapter.subprocess.run", side_effect=fake_run):
            candidate, _ = generate_title("codex.exe",app.DEFAULTS,{"current_title":"旧标题","original_goal":"修复中文工具"},ROOT)
        self.assertEqual(candidate,expected)

    def test_fixture_evaluator_can_read_chinese_in_legacy_locale(self):
        env = os.environ | {'PYTHONUTF8':'0','PYTHONIOENCODING':'utf-8'}
        p = subprocess.run([sys.executable,str(ROOT/'scripts/evaluate_naming.py')],
            capture_output=True, encoding='utf-8', env=env, timeout=10)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertIn('30',p.stdout)


if __name__ == '__main__':
    unittest.main()
