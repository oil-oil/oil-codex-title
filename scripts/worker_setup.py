"""注册当前用户的队列消费者；仅授权插件队列，不更改 Codex 私有权限。"""
import json
import os
from pathlib import Path
import subprocess
import sys

from handoff import queue_dir, task_name
from worker_identity import owner_process, windows_identity


RUNNER = '''import json, os, re, subprocess, sys
from pathlib import Path
home = Path({home})
root = Path({root})
os.environ['CODEX_HOME'] = str(home)
os.environ['OIL_CODEX_TITLE_DATA'] = str(root)
for name in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CODEX_APP_TOOLS_PIPE_PATH'):
    os.environ.pop(name, None)
choices = []
for marketplace, name in (('mogoo-codex-title', 'mogoo-codex-title'),
                          ('mogoo-oil-title', 'mogoo-codex-title'),
                          ('mogoo-oil-title', 'oil-codex-title')):
    cache = home / 'plugins/cache' / marketplace / name
    for package in cache.glob('*'):
        try:
            meta = json.loads((package / '.codex-plugin/plugin.json').read_text(encoding='utf-8'))
            match = re.fullmatch(r'(\\d+)\\.(\\d+)\\.(\\d+)(?:[-+].*)?', meta['version'])
            entry = package / 'scripts/mogoo_codex_title.py'
            if not entry.is_file():
                entry = package / 'scripts/oil_codex_title.py'
            if meta['name'] == name and match and (package / 'scripts/handoff.py').is_file() and entry.is_file():
                choices.append((name == 'mogoo-codex-title', tuple(map(int, match.groups())), str(entry)))
        except (OSError, ValueError, KeyError):
            pass
if choices:
    entry = max(choices)[2]
    subprocess.run([sys.executable, '-X', 'utf8', entry, 'worker'], check=False)
'''


def setup(root, config, plugin_root, *, remove=False):
    if os.name != 'nt' or not owner_process(config):
        raise ValueError('请在 Windows 当前登录用户身份下配置 worker')
    identity = windows_identity()
    root = Path(root).resolve()
    home = Path(os.environ.get('CODEX_HOME') or str(Path(os.environ['USERPROFILE']) / '.codex')).resolve()
    runner = root / 'worker_runner.py'
    root.mkdir(parents=True, exist_ok=True)
    if not remove:
        runner.write_text(RUNNER.format(home=repr(str(home)), root=repr(str(root))), encoding='utf-8')
    python = Path(sys.executable).with_name('pythonw.exe')
    if not python.is_file():
        python = Path(sys.executable)
    args = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
            str(Path(plugin_root) / 'scripts/install-worker.ps1'), '-QueueDir', str(queue_dir(root)),
            '-TaskName', task_name(root), '-PythonPath', str(python), '-RunnerPath', str(runner)]
    if remove:
        args.append('-Remove')
    proc = subprocess.run(args, capture_output=True, encoding='utf-8', errors='replace', timeout=30,
                          creationflags=subprocess.CREATE_NO_WINDOW)
    if proc.returncode:
        raise OSError('用户 worker 配置失败；请检查计划任务和插件队列目录权限')
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    report['owner_sid'] = None if remove else identity['sid']
    return report


def hydrate_relay_key(config):
    if os.name != 'nt' or config.get('provider') != 'relay':
        return
    import winreg
    name = config['relay']['api_key_env']
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            value, _ = winreg.QueryValueEx(key, name)
        if isinstance(value, str) and value.strip():
            os.environ[name] = value
    except OSError:
        # 进程级环境变量仍可使用；缺失时调用层明确报错，不回退官方账号。
        pass
