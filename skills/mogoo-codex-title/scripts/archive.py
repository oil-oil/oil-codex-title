"""定位 mogoo-codex-title 插件中的归档程序。"""
from pathlib import Path
import runpy
import sys

scripts = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(scripts))
runpy.run_path(str(scripts / "archive_policy.py"), run_name="__main__")
