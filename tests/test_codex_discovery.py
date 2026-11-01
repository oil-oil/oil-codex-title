"""合成安装目录模拟桌面更新和失效入口；不读取账号或调用模型。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import oil_codex_title as app
from codex_adapter import BackendError, find_codex


class CodexDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='codex-discovery-')
        self.addCleanup(self.tmp.cleanup)
        self.root = (Path(self.tmp.name) / '中文 空格').resolve()
        self.root.mkdir()
        self.paths = {}
        self.failures = {}
        # sys 是共享模块；只替换适配器引用，文件锁仍使用真实宿主平台。
        self.start_patch('codex_adapter.sys', SimpleNamespace(platform='win32'))
        self.start_patch('codex_adapter.platform.machine', return_value='AMD64')
        self.start_patch('codex_adapter.process_options', return_value={})
        environment = patch.dict(os.environ, {
            'APPDATA': str(self.root / 'roaming'),
            'LOCALAPPDATA': str(self.root / 'local'),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.start_patch('codex_adapter.shutil.which', side_effect=self.which)
        self.run = self.start_patch('codex_adapter.subprocess.run', side_effect=self.probe)

    def start_patch(self, *args, **kwargs):
        item = patch(*args, **kwargs)
        result = item.start()
        self.addCleanup(item.stop)
        return result

    def which(self, name):
        return self.paths.get(name) or (name if Path(name).is_file() else None)

    def probe(self, args, **kwargs):
        self.assertEqual(args[1:], ['--version'])
        self.assertFalse(kwargs.get('shell', False))
        self.assertLessEqual(kwargs['timeout'], 3)
        failure = self.failures.get(args[0])
        if isinstance(failure, Exception):
            raise failure
        return failure or SimpleNamespace(returncode=0, stdout='codex-cli 1.0.0\n')

    def file(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'synthetic executable')
        return str(path)

    def npm(self):
        shim = self.file('roaming/npm/codex.cmd')
        native = self.file('roaming/npm/node_modules/@openai/codex/node_modules/'
                           '@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe')
        return shim, native

    def configure(self, entry):
        return app.update_config(self.root / 'data', 'configure', SimpleNamespace(
            provider=None, model=None, service_tier=None, base_url=None,
            api_key_env=None, codex_bin=entry))

    def test_valid_explicit_entry_keeps_priority(self):
        explicit = self.file('custom/codex.exe')
        self.paths['codex.exe'] = self.file('other/codex.exe')
        self.assertEqual(find_codex(explicit), explicit)
        self.run.assert_called_once()
        self.assertEqual(self.run.call_args.args[0][0], explicit)

    def test_removed_desktop_version_falls_back_to_path(self):
        current = self.file('path/codex.exe')
        self.paths['codex.exe'] = current
        self.assertEqual(find_codex(str(self.root / 'removed-version/codex.exe')), current)

    def test_background_task_without_updated_path_finds_npm(self):
        _, native = self.npm()
        self.assertEqual(find_codex(str(self.root / 'removed-version/codex.exe')), native)

    def test_desktop_update_is_discovered_again_each_run(self):
        old = self.file('local/OpenAI/Codex/bin/old-version/codex.exe')
        self.assertEqual(find_codex(), old)
        Path(old).unlink()
        new = self.file('local/OpenAI/Codex/bin/new-version/codex.exe')
        self.assertEqual(find_codex(old), new)
        self.assertEqual(find_codex(), new)

    def test_newest_desktop_binary_is_preferred(self):
        old = self.file('local/OpenAI/Codex/bin/z-old/codex.exe')
        new = self.file('local/OpenAI/Codex/bin/a-new/codex.exe')
        os.utime(old, (100, 100))
        os.utime(new, (200, 200))
        self.assertEqual(find_codex(), new)

    def test_incomplete_desktop_update_skips_unlaunchable_binary(self):
        old = self.file('local/OpenAI/Codex/bin/old/codex.exe')
        new = self.file('local/OpenAI/Codex/bin/new/codex.exe')
        os.utime(old, (100, 100))
        os.utime(new, (200, 200))
        self.failures[new] = OSError('synthetic launch failure')
        self.assertEqual(find_codex(), old)

    def test_unknown_path_shim_does_not_block_known_npm_install(self):
        self.paths['codex'] = self.file('unknown/codex.cmd')
        _, native = self.npm()
        self.assertEqual(find_codex(), native)

    def test_failed_probes_fall_back_without_shell(self):
        broken = self.file('broken/codex.exe')
        self.paths['codex.exe'] = broken
        _, native = self.npm()
        failures = [OSError('synthetic failure'),
                    subprocess.TimeoutExpired([broken, '--version'], 3),
                    SimpleNamespace(returncode=1, stdout='codex-cli 1.0.0'),
                    SimpleNamespace(returncode=0, stdout='unrelated executable')]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.failures[broken] = failure
                self.assertEqual(find_codex(), native)

    def test_duplicate_native_candidates_are_probed_once(self):
        shim, native = self.npm()
        self.paths.update({'codex.exe': native, 'codex': shim, 'codex.cmd': shim})
        self.failures[native] = OSError('synthetic failure')
        with self.assertRaisesRegex(BackendError, '未找到可用'):
            find_codex()
        self.run.assert_called_once()

    def test_missing_install_fails_without_starting_process(self):
        with self.assertRaisesRegex(BackendError, '未找到可用'):
            find_codex()
        self.run.assert_not_called()

    def test_strict_configuration_rejects_typo_instead_of_falling_back(self):
        self.npm()
        app.atomic_json(self.root / 'data/config.json', {'future': 7})
        before = (self.root / 'data/config.json').read_bytes()
        with self.assertRaisesRegex(BackendError, '指定的 Codex 入口不可用'):
            self.configure(str(self.root / 'typo/codex.exe'))
        self.run.assert_not_called()
        self.assertEqual((self.root / 'data/config.json').read_bytes(), before)

    def test_auto_configuration_clears_version_path_and_preserves_settings(self):
        self.npm()
        saved = {'codex_bin': str(self.root / 'removed/codex.exe'), 'future': 7,
                 'model': 'synthetic-model', 'enabled': False, 'show_last_user_time': False}
        app.atomic_json(self.root / 'data/config.json', saved)
        config = self.configure('auto')
        self.assertIsNone(config['codex_bin'])
        for key in ('future', 'model', 'enabled', 'show_last_user_time'):
            self.assertEqual(config[key], saved[key])
        self.assertIsNone(app.read_json(self.root / 'data/config.json')['codex_bin'])

    def test_configure_preserves_npm_shim_instead_of_resolved_native_path(self):
        shim, native = self.npm()
        config = self.configure(shim)
        self.assertEqual(config['codex_bin'], shim)
        self.assertEqual(find_codex(config['codex_bin']), native)

    def test_configure_preserves_path_command_name(self):
        native = self.file('path/codex.exe')
        self.paths['codex.exe'] = native
        config = self.configure('codex.exe')
        self.assertEqual(config['codex_bin'], 'codex.exe')
        self.assertEqual(find_codex(config['codex_bin']), native)

    def test_fallback_does_not_rewrite_old_configuration(self):
        _, native = self.npm()
        app.atomic_json(self.root / 'data/config.json', {'codex_bin': str(self.root / 'old/codex.exe')})
        before = (self.root / 'data/config.json').read_bytes()
        config = app.load_config(self.root / 'data')
        self.assertEqual(find_codex(config['codex_bin']), native)
        self.assertEqual((self.root / 'data/config.json').read_bytes(), before)

    def test_linux_missing_explicit_entry_falls_back_to_path(self):
        native = self.file('posix/codex')
        self.paths['codex'] = native
        with patch('codex_adapter.sys.platform', 'linux'):
            self.assertEqual(find_codex(str(self.root / 'removed/codex')), native)

    def test_macos_keeps_desktop_app_priority_before_path(self):
        desktop = self.file('macos/Codex.app/Contents/Resources/codex')
        self.paths[str(Path('/Applications') / 'Codex.app/Contents/Resources/codex')] = desktop
        self.paths['codex'] = self.file('posix/codex')
        with patch('codex_adapter.sys.platform', 'darwin'):
            self.assertEqual(find_codex(), desktop)


if __name__ == '__main__':
    unittest.main()
