"""通过官方 App Server 协议读取/改名，通过临时 CLI 会话独立命名。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from usage_ledger import begin_attempt, parse_usage


class BackendError(RuntimeError):
    pass


class ModelSkipped(BackendError):
    """调用前发现状态已改变；不是模型失败，也不自动重试。"""
    def __init__(self, status):
        super().__init__(status)
        self.status = status


def process_options():
    # 隐藏后台 Codex 子进程的控制台窗口；不通过 shell 执行模型参数。
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def windows_binary(path):
    """将标准 npm 的 cmd 入口解析为原生 exe，避免经 cmd.exe 转义 JSON 参数。"""
    entry = Path(path)
    if entry.suffix.lower() == ".exe":
        return str(entry)
    if entry.suffix.lower() not in (".cmd", ".bat"):
        raise BackendError("Windows 需要原生 codex.exe；请通过 configure --codex-bin 指定路径")
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    target = "aarch64-pc-windows-msvc" if arch == "arm64" else "x86_64-pc-windows-msvc"
    package = (entry.parent / "node_modules/@openai/codex").resolve()
    roots = [package, package / "node_modules/@openai" / ("codex-win32-" + arch),
             entry.parent / "node_modules/@openai" / ("codex-win32-" + arch)]
    # 兼容 pnpm 链接后的包根目录以及 npm 的可选依赖布局。
    roots.extend(parent / ("codex-win32-" + arch) for parent in package.parents)
    for root in roots:
        for folder in ("bin", "codex"):
            candidate = root / "vendor" / target / folder / "codex.exe"
            if candidate.is_file():
                return str(candidate)
    raise BackendError("未找到 npm 入口对应的 codex.exe；请重装 Codex CLI 或通过 configure --codex-bin 指定原生路径")


def codex_responds(binary):
    """确认候选入口可运行，避免 WindowsApps 的受限 exe 抢先命中。"""
    try:
        result = subprocess.run([binary, "--version"], capture_output=True,
                                encoding="utf-8", timeout=10, **process_options())
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def find_codex(explicit: str | None = None) -> str:
    if explicit:
        resolved = shutil.which(explicit)
        if resolved:
            binary = windows_binary(resolved) if sys.platform == "win32" else resolved
            if sys.platform == "win32" and not codex_responds(binary):
                raise BackendError("配置的 Codex CLI 无法执行 --version；请用 configure --codex-bin 指定可用的原生 codex.exe")
            return binary
        raise BackendError("配置的 Codex 可执行文件不存在")
    if sys.platform == "darwin":
        for base in (Path("/Applications"), Path.home() / "Applications"):
            for app in ("ChatGPT.app", "Codex.app"):
                candidate = base / app / "Contents/Resources/codex"
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
    if sys.platform == "win32":
        # PATH 中的 WindowsApps exe 可能存在但拒绝执行；继续尝试 npm 的 cmd 入口。
        seen = set()
        found = False
        for name in ("codex", "codex.cmd", "codex.exe"):
            path = shutil.which(name)
            if not path:
                continue
            found = True
            try:
                binary = windows_binary(path)
            except BackendError:
                continue
            if binary in seen:
                continue
            seen.add(binary)
            if codex_responds(binary):
                return binary
        if found:
            raise BackendError("发现 Codex CLI 入口但无法执行；请用 configure --codex-bin 指定可用的原生 codex.exe")
        raise BackendError("未找到 Windows Codex CLI；请将 codex.exe 加入 PATH，或通过 configure --codex-bin 指定路径")
    path = shutil.which("codex")
    if not path:
        raise BackendError("未找到 Codex；请安装并登录，或配置 codex_bin")
    return path


def worker_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_APP_TOOLS_PIPE_PATH"):
        env.pop(key, None)
    env["OIL_CODEX_TITLE_WORKER"] = "1"
    return env


class CodexBackend:
    """独立 stdio 连接；不会 resume 原会话或向它发送 turn/start。"""
    def __init__(self, binary: str, timeout: float = 15, *, disable_hooks: bool = True):
        self.binary = binary
        self.timeout = timeout
        self.proc = None
        self.messages = queue.Queue()
        self.counter = 0
        self.disable_hooks = disable_hooks

    def __enter__(self):
        self.proc = subprocess.Popen(
            [self.binary, "app-server"] + (["--disable", "hooks"] if self.disable_hooks else []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", env=worker_env(), **process_options(),
        )
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            self.call("initialize", {
                "clientInfo": {"name": "oil-codex-title", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            })
            self._send({"method": "initialized"})
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def _reader(self):
        try:
            for line in self.proc.stdout:
                self.messages.put(json.loads(line))
        except (ValueError, OSError):
            pass
        finally:
            self.messages.put(None)

    def _send(self, value):
        self.proc.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def call(self, method, params):
        self.counter += 1
        request_id = self.counter
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise BackendError("App Server 请求超时：" + method) from exc
            if message is None:
                raise BackendError("App Server 连接已关闭")
            if message.get("id") != request_id:
                if time.monotonic() > deadline:
                    raise BackendError("App Server 请求超时：" + method)
                continue
            if "error" in message:
                code = message["error"].get("code")
                raise BackendError(f"App Server {method} 失败（{code}）；请用兼容的桌面版本运行 doctor")
            return message["result"]

    def read(self, thread_id):
        return self.call("thread/read", {"threadId": thread_id, "includeTurns": True})["thread"]

    def rename(self, thread_id, title):
        return self.call("thread/name/set", {"threadId": thread_id, "name": title})

    def list_threads(self, *, archived=False, cwd=None):
        """分页读取官方列表，不将归档话题或遗漏页混入候选。"""
        params = {"archived": archived, "limit": 100, "sourceKinds": ["cli", "vscode", "appServer", "exec"],
                  "modelProviders": []}
        if archived:
            params["sourceKinds"] += ["unknown", "subAgent", "subAgentReview", "subAgentCompact",
                                      "subAgentThreadSpawn", "subAgentOther"]
        if cwd:
            params["cwd"] = cwd
        seen, cursors = set(), set()
        while True:
            page = self.call("thread/list", params)
            for thread in page["data"]:
                if thread["id"] not in seen:
                    seen.add(thread["id"])
                    yield thread
            cursor = page.get("nextCursor")
            if not cursor:
                return
            if cursor in cursors:
                raise BackendError("话题列表分页游标重复；停止扫描")
            cursors.add(cursor)
            params["cursor"] = cursor

    def is_archived(self, thread_id, cwd=None):
        return any(t["id"] == thread_id for t in self.list_threads(archived=True, cwd=cwd))

    def __exit__(self, *_):
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=3)
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream:
                stream.close()


SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["keep", "rename"]},
        "title": {"type": "string"}, "reason": {"type": "string"},
    },
    "required": ["action", "title", "reason"],
}


def normalize_project_prefix(candidate, context):
    """去掉与外层目录精确等价的重复前缀，不猜项目别名或修改 keep。"""
    hint = re.sub(r"[\W_]+", "", context.get("project_hint", ""))
    if candidate.get("action") != "rename" or not hint or " " not in candidate.get("title", ""):
        return candidate
    emoji, body = candidate["title"].split(" ", 1)
    # 兼容 kite-lms、Kite LMS、KiteLMS，不把 Maple 错当成 MaplePay。
    pattern = r"^" + r"[\s._-]*".join(re.escape(c) for c in hint) + r"\s+(.+)$"
    match = re.match(pattern, body, re.IGNORECASE)
    if not match:
        return candidate
    remaining = match.group(1).strip()
    if len(remaining) < 2 or re.match(r"^(与|和|到|及|→|->|vs\b|to\b)", remaining, re.IGNORECASE):
        return candidate
    return {**candidate, "title": emoji + " " + remaining}


def _generate_title_once(binary, config, context, plugin_root, *, before_model=None):
    # 只有问候/确认时没有命名证据；确定性保留，避免模型凭空生成“普通讨论”。
    trivial = {"", "你好", "您好", "hi", "hello", "嗨", "谢谢", "好的", "好", "ok", "收到", "继续", "嗯"}
    user_texts = [context.get("original_goal", "")] + [
        message.get("text", "") for turn in context.get("recent_turns", [])
        for message in turn.get("messages", []) if message.get("role") == "user"
    ]
    if all(re.sub(r"[\W_]+", "", text).casefold() in trivial for text in user_texts):
        return {"action": "keep", "title": context.get("current_title", ""),
                "reason": "只有问候或确认，缺少新的命名依据"}, {}
    result, usage = generate_json(binary, config, context, plugin_root / "prompts/naming.md", SCHEMA,
                                 before_model=before_model)
    return normalize_project_prefix(result, context), usage


def generate_json(binary, config, context, policy, output_schema, *, before_model=None):
    """隔离的无工具临时模型，供命名和归档评估共用。"""
    deadline = time.monotonic() + config["model_timeout_seconds"]
    # 复用当前登录；不复制凭据，不恢复原会话，不保留独立会话记录。
    with tempfile.TemporaryDirectory(prefix="oil-codex-title-") as tmp:
        temp = Path(tmp)
        schema = temp / "schema.json"
        schema.write_text(json.dumps(output_schema), encoding="utf-8")
        output = temp / "result.json"
        args = [
            binary, "exec", "--ephemeral", "--ignore-user-config",
            "--skip-git-repo-check", "--sandbox", "read-only", "-C", tmp,
            "--disable", "hooks", "--disable", "shell_tool",
            "--disable", "plugins", "--disable", "apps", "--disable", "multi_agent",
            "-m", config["model"], "-c", 'model_reasoning_effort="low"',
            "-c", "project_doc_max_bytes=0", "-c", "skills.max_context_tokens=1",
            "-c", "agents.enabled=false", "-c", 'web_search="disabled"',
            "-c", "apps._default.enabled=false",
            "-c", "model_instructions_file=" + json.dumps(str(policy)),
            "--output-schema", str(schema), "--output-last-message", str(output),
            "--json", "-",
        ]
        if config.get("service_tier"):
            args[2:2] = ["-c", "service_tier=" + json.dumps(config["service_tier"])]
        # 配额等待和每次内部重试之后，紧接真实模型进程启动前复核。
        if before_model:
            before_model()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BackendError("状态复核已耗尽本次模型时间预算")
        attempt = begin_attempt(config)
        usage, status = {}, "interrupted"
        try:
            proc = subprocess.run(args, input=json.dumps(context, ensure_ascii=False),
                                  capture_output=True, encoding="utf-8", env=worker_env(),
                                  timeout=remaining, **process_options())
            usage, forbidden_tool = parse_usage(proc.stdout)
            status = "process_error"
            if proc.returncode or not output.exists():
                raise BackendError("独立命名模型失败；请检查登录、模型配置和 doctor")
            status = "rejected_tool"
            if forbidden_tool:
                raise BackendError("命名模型尝试调用工具，本次结果已丢弃")
            status = "invalid_json"
            result = json.loads(output.read_text(encoding="utf-8"))
            status = "completed"
            return result, usage
        except subprocess.TimeoutExpired as exc:
            usage, _ = parse_usage(exc.stdout)
            status = "timeout"
            raise BackendError("独立命名模型超时；原标题保留") from exc
        except OSError:
            status = "process_error"
            raise
        finally:
            if attempt:
                attempt.finish(status, usage)


def generate_title(binary, config, context, plugin_root, *, before_model=None):
    deadline = time.monotonic() + config.get("model_timeout_seconds", 100)
    candidate, usage = _generate_title_once(binary, config, context, plugin_root, before_model=before_model)
    current = context.get("current_title", "")
    legacy = current.count("｜") != 1 or current.startswith("🛠")
    # 模型偶尔误把旧标题判断为结构合规。只复核一次，不自行猜对象或强制改名。
    # 问候过滤不调用模型且无 usage，仍然直接保留。
    remaining = deadline - time.monotonic()
    if candidate.get("action") == "keep" and legacy and usage and remaining > 0:
        # 格式复核共享首次生成的时间预算，不能使 Hook 的最坏耗时翻倍。
        retry_config = {**config, "model_timeout_seconds": remaining}
        candidate, retry_usage = _generate_title_once(binary, retry_config, {
            **context,
            "naming_feedback": "原标题尚未符合 emoji 对象｜目标结构，或仍使用旧开发图标。请重新核对：有明确对象和目标时只迁移格式，保留准确主线；只有依据不足时 keep。不要误称旧格式已合规。",
        }, plugin_root, before_model=before_model)
        usage = {key: usage.get(key, 0) + retry_usage.get(key, 0)
                 for key in usage.keys() | retry_usage.keys()}
    return candidate, usage
