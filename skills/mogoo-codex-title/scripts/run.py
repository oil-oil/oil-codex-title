"""定位所属插件，调用共享程序，避免 Skill 复制后台实现。"""
from pathlib import Path
import runpy
import sys

scripts = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(scripts))
runpy.run_path(str(scripts / "mogoo_codex_title.py"), run_name="__main__")
