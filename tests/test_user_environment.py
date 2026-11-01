"""密钥写入使用合成值和独立临时注册表项，不修改用户真实环境变量。"""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import user_environment as environment


class UserEnvironmentTests(unittest.TestCase):
    def test_invalid_input_is_rejected_without_including_key_value(self):
        for name, value in (("PATH", "synthetic-key"), ("pythonpath", "synthetic-key"), ("", "synthetic-key"),
                            ("bad-name", "synthetic-key"), ("TITLE_TEST_KEY", ""),
                            ("TITLE_TEST_KEY", "synthetic-key\x00"), ("TITLE_TEST_KEY", "synthetic-key\t")):
            with self.subTest(name=name), self.assertRaises(ValueError) as error:
                environment.validate_key_input(name, value)
            self.assertNotIn("synthetic-key", str(error.exception))

    def test_windows_registry_failure_does_not_change_process_environment(self):
        registry = MagicMock()
        registry.SetValueEx.side_effect = OSError("synthetic failure")
        with patch.object(environment.sys, "platform", "win32"), patch.dict(sys.modules, {"winreg": registry}), \
             patch.dict(os.environ, {"TITLE_ENV_TEST_KEY": "old-synthetic-key"}), \
             patch.object(environment, "notify_environment_change") as notify:
            with self.assertRaises(OSError):
                environment.persist_user_key("TITLE_ENV_TEST_KEY", "new-synthetic-key")
            self.assertEqual(os.environ["TITLE_ENV_TEST_KEY"], "old-synthetic-key")
            notify.assert_not_called()

    def test_unsupported_platform_keeps_environment_unchanged(self):
        with patch.object(environment.sys, "platform", "linux"), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                environment.persist_user_key("TITLE_ENV_TEST_KEY", "synthetic-key")
            self.assertNotIn("TITLE_ENV_TEST_KEY", os.environ)

    @unittest.skipUnless(sys.platform == "win32", "真实 winreg 路径验收仅在 Windows 执行")
    def test_real_registry_write_and_readback_with_isolated_test_subkey(self):
        import winreg
        subkey = "Software\\oil-title-tests\\" + uuid.uuid4().hex
        try:
            with patch.object(environment, "ENVIRONMENT_SUBKEY", subkey), \
                 patch.object(environment, "notify_environment_change") as notify, patch.dict(os.environ):
                environment.persist_user_key("TITLE_ENV_TEST_KEY", "synthetic-registry-key")
                self.assertEqual(os.environ["TITLE_ENV_TEST_KEY"], "synthetic-registry-key")
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
                    value, kind = winreg.QueryValueEx(key, "TITLE_ENV_TEST_KEY")
                    self.assertEqual((value, kind), ("synthetic-registry-key", winreg.REG_SZ))
                notify.assert_called_once_with()
        finally:
            # 测试生成的独立注册表项，与 Windows 用户 Environment 项完全分离。
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)


if __name__ == "__main__":
    unittest.main()
