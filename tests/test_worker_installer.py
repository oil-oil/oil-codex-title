"""真实 PowerShell 编码与安装入口回归；任务和 ACL 操作由合成函数替代。"""
import base64
import codecs
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install-worker.ps1"
HARNESS = ROOT / "tests/fixtures/worker_installer_harness.ps1"
DESCRIPTION = "以当前用户身份消费标题插件的队列，不执行队列提供的命令"
WINDOWS_PS = shutil.which("powershell.exe") if os.name == "nt" else None
POWERSHELL_7 = shutil.which("pwsh.exe") if os.name == "nt" else None

PARSE = r"""
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$tokensFound = $null
$errorsFound = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:MOGOO_TITLE_TEST_INSTALLER, [ref]$tokensFound, [ref]$errorsFound)
$command = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.CommandAst] -and
    $node.GetCommandName() -eq 'Register-ScheduledTask'
}, $true)
$description = $null
if ($command) {
    for ($i = 0; $i -lt $command.CommandElements.Count - 1; $i++) {
        if ($command.CommandElements[$i].ParameterName -eq 'Description') {
            $description = $command.CommandElements[$i + 1].Value
        }
    }
}
@{ errors = @($errorsFound | ForEach-Object { $_.ErrorId }); description = $description;
   major_version = $PSVersionTable.PSVersion.Major } | ConvertTo-Json -Compress
"""


class WorkerInstallerTests(unittest.TestCase):
    def run_shell(self, binary, args, **kwargs):
        proc = subprocess.run([binary, "-NoProfile", "-NonInteractive", *args],
                              capture_output=True, encoding="utf-8", errors="replace", timeout=20,
                              creationflags=subprocess.CREATE_NO_WINDOW, **kwargs)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def test_installer_keeps_explicit_utf8_signature(self):
        raw = INSTALLER.read_bytes()
        self.assertTrue(raw.startswith(codecs.BOM_UTF8), "Windows PowerShell 5.1 需要 BOM 识别中文 UTF-8 脚本")
        self.assertIn(DESCRIPTION, raw.decode("utf-8-sig"))

    def check_parser(self, binary, major):
        encoded = base64.b64encode(PARSE.encode("utf-16le")).decode("ascii")
        report, = self.run_shell(binary, ["-EncodedCommand", encoded],
                                env=os.environ | {"MOGOO_TITLE_TEST_INSTALLER": str(INSTALLER)})
        self.assertEqual(report["major_version"], major)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["description"], DESCRIPTION)

    def check_installation(self, binary):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            queue = root / "MogooOilCodexTitle/0123456789abcdef/queue"
            python = root / "解释器 空格/pythonw.exe"
            runner = root / "数据 空格/worker_runner.py"
            status, report = self.run_shell(binary, ["-File", str(HARNESS), "-InstallerPath", str(INSTALLER),
                "-ProgramDataRoot", str(root), "-QueueDir", str(queue),
                "-PythonPath", str(python), "-RunnerPath", str(runner)])
            self.assertEqual(status, {"status": "installed", "sandbox_queue_authorized": True})
            self.assertEqual(report["registered"], 1)
            self.assertEqual(report["acl_calls"], 1)
            self.assertEqual(report["acl_arguments"], [str(queue), "/grant",
                "*S-1-5-21-123456789-123456789-123456789-1001:(OI)(CI)M", "/T"])
            self.assertEqual(report["task"]["name"], "MogooOilCodexTitleWorker-0123456789abcdef")
            self.assertEqual(report["task"]["description"], DESCRIPTION)
            self.assertEqual(report["task"]["action"], {"Execute": str(python),
                                                      "Arguments": '-X utf8 "' + str(runner) + '"'})
            self.assertEqual(report["task"]["principal"], {"LogonType": "Interactive", "RunLevel": "Limited"})
            self.assertEqual(report["task"]["trigger"]["IntervalSeconds"], 60)
            for name in ("pending", "working", "done", "locks"):
                self.assertTrue((queue / name).is_dir())

    @unittest.skipUnless(WINDOWS_PS, "需要 Windows PowerShell 5.1")
    def test_windows_powershell_51_parses_file_and_preserves_chinese(self):
        self.check_parser(WINDOWS_PS, 5)

    @unittest.skipUnless(POWERSHELL_7, "需要 Windows 下的 PowerShell 7")
    def test_powershell_7_parses_file_and_preserves_chinese(self):
        self.check_parser(POWERSHELL_7, 7)

    @unittest.skipUnless(WINDOWS_PS, "需要 Windows PowerShell 5.1")
    def test_windows_powershell_51_executes_installer_with_mocked_system_writes(self):
        self.check_installation(WINDOWS_PS)

    @unittest.skipUnless(POWERSHELL_7, "需要 Windows 下的 PowerShell 7")
    def test_powershell_7_executes_installer_with_mocked_system_writes(self):
        self.check_installation(POWERSHELL_7)


if __name__ == "__main__":
    unittest.main()
