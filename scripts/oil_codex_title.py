#!/usr/bin/env python3
"""话题自动命名入口；Hook 始终只向宿主返回空 JSON。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid

from codex_adapter import BackendError, ModelSkipped, CodexBackend, find_codex, generate_title, process_options
import file_lock
from usage_ledger import usage_scope, usage_report

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    "enabled": True,
    "model": "gpt-6-luna",
    "service_tier": "priority",
    "codex_bin": None,
    "recent_turns": 5,
    "max_context_chars": 14000,
    "model_timeout_seconds": 100,
    "max_parallel_workers": 2,
}
EMOJI = ("🎬", "🧩", "🔎", "📝", "📅", "🎨", "⚙️", "💬")
POLICY_VERSION = 8


def data_dir():
    override = os.environ.get("OIL_CODEX_TITLE_DATA")
    return Path(override).expanduser() if override else Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    ) / "oil-codex-title"


def read_json(path, default=None):
    if not path.exists():
        return {} if default is None else default
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON 文件必须是对象")
    return value


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(filename, path)
    finally:
        if os.path.exists(filename):
            os.unlink(filename)


def load_config(root):
    config = DEFAULTS | read_json(root / "config.json")
    if not isinstance(config["enabled"], bool):
        raise ValueError("enabled 必须是布尔值")
    for key, lower, upper in (("recent_turns", 3, 5), ("max_context_chars", 3000, 20000),
                              ("model_timeout_seconds", 10, 110), ("max_parallel_workers", 1, 8)):
        if type(config[key]) is not int or not lower <= config[key] <= upper:
            raise ValueError(f"{key} 必须在 {lower}～{upper} 之间")
    if not isinstance(config["model"], str) or not config["model"].strip():
        raise ValueError("model 不能为空")
    if config["service_tier"] not in (None, "priority"):
        raise ValueError("service_tier 必须是 null（标准）或 priority（Fast）")
    return config


def valid_id(value):
    return str(uuid.UUID(value))


@contextmanager
def thread_lock(root, thread_id, wait_seconds=0):
    # 内核锁在进程结束后释放，避免崩溃留下永久锁或两个 Worker 覆盖结果。
    path = root / "locks" / (thread_id + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                file_lock.acquire(stream)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        try:
            yield True
        finally:
            file_lock.release(stream)


@contextmanager
def worker_slot(root, limit, wait_seconds):
    """不同话题共享进程池配额，等待时间算入模型预算。"""
    deadline = time.monotonic() + wait_seconds
    while True:
        for index in range(limit):
            with thread_lock(root / "worker-pool", str(index)) as acquired:
                if acquired:
                    yield True
                    return
        if time.monotonic() >= deadline:
            yield False
            return
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def limited_title(binary, root, config, context, *, before_model=None):
    deadline = time.monotonic() + config["model_timeout_seconds"]
    with worker_slot(root, config["max_parallel_workers"], config["model_timeout_seconds"]) as acquired:
        remaining = deadline - time.monotonic()
        if not acquired or remaining <= 0:
            raise BackendError("后台命名并发已满；本次保留原标题")
        return generate_title(binary, {**config, "model_timeout_seconds": remaining}, context, ROOT,
                              before_model=before_model)


def ensure_title_active(backend, thread_id, root):
    if backend.is_archived(thread_id):
        raise ModelSkipped("archived")
    if not load_config(root)["enabled"]:
        raise ModelSkipped("disabled")
    if read_json(state_path(root, thread_id)).get("locked"):
        raise ModelSkipped("locked")


def read_settled_thread(backend, thread_id, event_turn, timeout=5):
    """确认 Stop 对应轮次已完成；异步保存尚未完成时短暂轮询。"""
    deadline = time.monotonic() + timeout
    while True:
        thread = backend.read(thread_id)
        turns = thread.get("turns", [])
        if not turns or turns[-1]["id"] != event_turn:
            return thread, "outdated_event"
        status = turns[-1].get("status")
        if status == "completed":
            return thread, None
        if status in ("failed", "interrupted"):
            return thread, "unfinished_turn"
        if time.monotonic() >= deadline:
            return thread, "turn_not_settled"
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def state_path(root, thread_id):
    return root / "threads" / (thread_id + ".json")


def audit(root, thread_id, result):
    path = root / "logs" / (thread_id + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 256 * 1024:
        os.replace(path, path.with_suffix(".previous.jsonl"))
    # 不保存对话原文、模型提示词、推理或 CLI 原始 stderr。
    entry = {"time": int(time.time()), **result}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def clean_text(text):
    # 剔除宿主注入的浏览器状态，保留真实用户请求。
    text = re.sub(r"<in-app-browser-context\b[^>]*>[\s\S]*?</in-app-browser-context>", "", text)
    text = re.sub(r"<environment_context>[\s\S]*?</environment_context>", "", text)
    for tag in ("recommended_plugins", "skills_instructions", "app-context", "skill"):
        text = re.sub(r"<" + tag + r"\b[^>]*>[\s\S]*?</" + tag + r">", "", text)
    # 附件说明和能力清单常出现在真实请求前，不能占满每条消息的摘录预算。
    request = re.search(r"(?m)^#{1,3} My request:\s*\n", text)
    if request:
        text = text[request.end():]
    return text.strip()


def project_hint(thread):
    cwd = thread.get("cwd")
    if not cwd:
        return ""
    path = Path(cwd)
    if path == Path.home() or path.parent.name.lower() in ("users", "home"):
        return ""
    name = path.name
    if name.lower() in ("desktop", "documents", "downloads", "tmp", "project", "projects"):
        return ""
    return name[:64] if re.fullmatch(r"[\w .-]{1,64}", name) else ""


def conflicting_titles(root, thread_id, candidate, scope_key=""):
    conflicts = set()
    for path in (root / "threads").glob("*.json"):
        if path.stem == thread_id:
            continue
        try:
            state = read_json(path)
        except (ValueError, OSError):
            continue
        if state.get("scope_key", "") != scope_key:
            continue
        other = state.get("last_seen_title")
        if other == candidate:
            conflicts.add(other)
    return sorted(conflicts)


def item_text(item):
    if item.get("type") == "userMessage":
        return clean_text("\n".join(
            part.get("text", "") for part in item.get("content", []) if part.get("type") == "text"
        ))
    return clean_text(item.get("text", ""))


def snapshot(thread, config):
    """只给命名模型用户与最终回答；用最新轮次 ID 检测生成期间的新活动。"""
    turns = thread.get("turns", [])
    effective = []
    for turn in turns:
        messages = []
        for item in turn.get("items", []):
            kind = item.get("type")
            if kind != "userMessage" and not (
                kind == "agentMessage" and item.get("phase") in (None, "final_answer")
            ):
                continue
            text = item_text(item)
            if text:
                messages.append({"role": "user" if kind == "userMessage" else "assistant", "text": text})
        if any(m["role"] == "user" for m in messages):
            effective.append({"id": turn["id"], "messages": messages})
    title = thread.get("name") or ""
    selected = effective[-config["recent_turns"]:]
    budget = config["max_context_chars"]
    # 为每轮的用户目标保留空间；助手长回答不能挤掉其他轮。
    per_message = max(200, (budget - 2000) // max(1, sum(len(t["messages"]) for t in selected)))
    recent = [{"id": t["id"], "messages": [
        {"role": m["role"], "text": m["text"][:min(per_message, 1200 if m["role"] == "user" else 500)]}
        for m in t["messages"]
    ]} for t in selected]
    original = ""
    if effective:
        original = next(m["text"] for m in effective[0]["messages"] if m["role"] == "user")[:800]
    latest_id = turns[-1]["id"] if turns else None
    context = {"current_title": title, "project_hint": project_hint(thread),
               "original_goal": original, "recent_turns": recent}
    signature = json.dumps({"policy_version": POLICY_VERSION, "project_hint": context["project_hint"],
                           "latest_id": latest_id, "effective": effective[-config["recent_turns"]:]},
                           ensure_ascii=False, sort_keys=True)
    fingerprint = hashlib.sha256(signature.encode()).hexdigest()
    scope_key = hashlib.sha256(str(thread["cwd"]).encode()).hexdigest() if thread.get("cwd") else ""
    return {"title": title, "latest_id": latest_id, "fingerprint": fingerprint,
            "context": context, "has_messages": bool(effective), "scope_key": scope_key}


def validate_candidate(candidate, current_title):
    if not isinstance(candidate, dict) or set(candidate) != {"action", "title", "reason"}:
        raise ValueError("模型输出字段无效")
    if candidate["action"] not in ("rename", "keep") or not all(
        isinstance(candidate[k], str) for k in ("title", "reason")
    ):
        raise ValueError("模型输出类型无效")
    if candidate["action"] == "keep":
        candidate = {**candidate, "title": current_title}
    else:
        title = candidate["title"]
        if title != title.strip() or not 4 <= len(title) <= 48:
            raise ValueError("标题长度或空白无效")
        if not any(title.startswith(e + " ") for e in EMOJI):
            raise ValueError("标题缺少允许的类别 emoji")
        body = title.split(" ", 1)[1]
        if body.count("｜") != 1 or "|" in body:
            raise ValueError("标题必须采用对象｜目标结构")
        if any(not part or part != part.strip() for part in body.split("｜")):
            raise ValueError("标题对象与目标不能为空或带边缘空格")
        if (not body.strip() or any(e in body for e in EMOJI)
                or any(0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF for c in body)):
            raise ValueError("标题正文无效或包含多个类别 emoji")
        if any(unicodedata.category(c).startswith("C") for c in title):
            raise ValueError("标题含控制字符")
        if re.search(r"[A-Za-z]:[\\/]|\\\\", title):
            raise ValueError("标题含 Windows 绝对路径")
        if any(x in title for x in ("\n", "\r", "`", "https://", "http://", "@", "/Users/", "sk-")):
            raise ValueError("标题含不允许的格式或私人信息")
    return {**candidate, "reason": candidate["reason"][:300]}


def confirmation_only(thread, state, config):
    """仅在基线仍一致时跳过最多两轮纯确认；附件、遗漏轮次或规则升级均重新判断。"""
    if state.get("policy_version") != POLICY_VERSION or not state.get("last_fingerprint"):
        return 0
    current = thread.get("name") or ""
    try:
        validate_candidate({"action": "rename", "title": current, "reason": ""}, current)
    except ValueError:
        return 0
    turns = thread.get("turns", [])
    index = next((i for i, t in enumerate(turns) if t["id"] == state.get("last_turn_id")), None)
    if index is None:
        return 0
    pending = turns[index + 1:]
    if not pending or len(pending) + state.get("confirmation_skips", 0) > 2:
        return 0
    if snapshot({**thread, "turns": turns[:index + 1]}, config)["fingerprint"] != state["last_fingerprint"]:
        return 0
    allowed = {"好", "好的", "可以", "收到", "谢谢", "继续", "ok", "okay", "thanks", "thank you", "continue"}
    for turn in pending:
        if turn.get("status") != "completed" or turn.get("itemsView", "full") != "full":
            return 0
        users = [m for m in turn.get("items", []) if m.get("type") == "userMessage"]
        if not users:
            return 0
        for message in users:
            parts = message.get("content", [])
            if not parts or any(p.get("type") != "text" for p in parts):
                return 0
            # 检查原始消息，不让宿主包装清理器吞掉附件说明或额外需求。
            raw = "\n".join(p.get("text", "") for p in parts)
            if raw.strip(" \t\r\n.!。！").casefold() not in allowed:
                return 0
    return len(pending)


def process_thread(backend, generator, thread_id, root, config, *, apply=False, event_turn=None):
    with usage_scope(root, "naming", thread_id) as accounting:
        result = _process_thread(backend, generator, thread_id, root, config, apply=apply, event_turn=event_turn)
        accounting.outcome = result["status"]
        return result


def _process_thread(backend, generator, thread_id, root, config, *, apply=False, event_turn=None):
    thread_id = valid_id(thread_id)
    if not config["enabled"]:
        return {"status": "disabled"}
    # 新轮次的 Hook 等待旧 Worker 释放锁，再判断是否已经过期，避免丢掉最新请求。
    with thread_lock(root, thread_id, config["model_timeout_seconds"] * 2 + 20 if event_turn else 0) as acquired:
        if not acquired:
            return {"status": "busy"}
        path = state_path(root, thread_id)
        state = read_json(path)
        if backend.is_archived(thread_id):
            return {"status": "archived"}
        if event_turn:
            thread, pending = read_settled_thread(backend, thread_id, event_turn)
            if pending:
                return {"status": pending}
        else:
            thread = backend.read(thread_id)
        before = snapshot(thread, config)
        if not before["has_messages"]:
            return {"status": "empty"}
        # 上次写入后进程被中断时，先核对待确认结果，避免误认作手工改名。
        if state.get("pending_title") == before["title"]:
            state.update(last_seen_title=before["title"], last_generated_title=before["title"])
            state.pop("pending_title", None)
            if apply:
                atomic_json(path, state)
        # 初次观察到的标题可能仍是宿主的临时标题。只有成功评估/写入后，
        # 才有稳定基线可用于保护外部改名；过期结果不能建立这条基线。
        established = bool(state.get("last_fingerprint") or state.get("last_generated_title"))
        if (state.get("locked") and state.get("lock_reason") == "检测到外部改名"
                and not established):
            state.update(locked=False, lock_reason="首次标题尚未建立稳定基线")
            if apply:
                atomic_json(path, state)
                audit(root, thread_id, {"status": "initial_baseline_recovered"})
        if state.get("locked"):
            return {"status": "locked", "title": before["title"]}
        if established and "last_seen_title" in state and state["last_seen_title"] != before["title"]:
            if apply:
                state.update(locked=True, lock_reason="检测到外部改名", last_seen_title=before["title"])
                atomic_json(path, state)
            return {"status": "manual_title", "title": before["title"]}
        if state.get("last_fingerprint") == before["fingerprint"]:
            return {"status": "unchanged", "title": before["title"]}
        skipped = confirmation_only(thread, state, config)
        try:
            ensure_title_active(backend, thread_id, root)
            if skipped:
                candidate, usage = {"action": "keep", "title": before["title"], "reason": "新增内容仅为确认，保留稳定标题"}, {}
            else:
                candidate, usage = generator(before["context"])
        except ModelSkipped as exc:
            return {"status": exc.status}
        candidate = validate_candidate(candidate, before["title"])
        conflicts = conflicting_titles(root, thread_id, candidate["title"], before["scope_key"])
        if candidate["action"] == "rename" and conflicts:
            try:
                ensure_title_active(backend, thread_id, root)
                candidate, retry_usage = generator({**before["context"], "conflicting_titles": conflicts,
                    "naming_feedback": "候选与已记录任务重名。用对话里真实的项目、模块或内容主题区分；无法区分就保留原名，不编造编号。"})
            except ModelSkipped as exc:
                return {"status": exc.status, "usage": usage}
            candidate = validate_candidate(candidate, before["title"])
            usage = {key: usage.get(key, 0) + retry_usage.get(key, 0)
                     for key in usage.keys() | retry_usage.keys()}
            if candidate["action"] == "rename" and conflicting_titles(root, thread_id, candidate["title"], before["scope_key"]):
                return {"status": "ambiguous_title", "title": before["title"], "usage": usage}
        result = {"status": "preview", **candidate, "usage": usage}
        if skipped:
            result["skip_reason"] = "confirmation_only"
        if not apply:
            return result
        # 模型运行期间用户可能发起下一轮、改名、暂停或锁定。
        fresh_config = load_config(root)
        if not fresh_config["enabled"]:
            return {"status": "disabled"}
        latest_state = read_json(path)
        if latest_state.get("locked"):
            return {"status": "locked"}
        if backend.is_archived(thread_id):
            return {"status": "archived"}
        after = snapshot(backend.read(thread_id), config)
        if after["title"] != before["title"] or after["fingerprint"] != before["fingerprint"]:
            return {"status": "stale_result"}
        state.update(last_seen_title=before["title"], last_turn_id=before["latest_id"],
                     scope_key=before["scope_key"], updated_at=int(time.time()),
                     policy_version=POLICY_VERSION,
                     confirmation_skips=state.get("confirmation_skips", 0) + skipped if skipped else 0)
        if candidate["action"] == "rename" and candidate["title"] != before["title"]:
            state["pending_title"] = candidate["title"]
            atomic_json(path, state)
            try:
                backend.rename(thread_id, candidate["title"])
            except BackendError:
                # 网络/进程错误可能发生在写入成功后，先读回确认，不盲目重试。
                if snapshot(backend.read(thread_id), config)["title"] != candidate["title"]:
                    raise
            verified = snapshot(backend.read(thread_id), config)
            if verified["title"] != candidate["title"]:
                raise BackendError("标题写入后核验不一致")
            state.update(last_seen_title=candidate["title"], last_generated_title=candidate["title"])
            state.pop("pending_title", None)
            result["status"] = "renamed"
            result["verification"] = "metadata_only"
        else:
            result["status"] = "kept"
        state["last_fingerprint"] = before["fingerprint"]
        atomic_json(path, state)
        audit(root, thread_id, result)
        return result


def windows_cli_preflight(version):
    """报告 Windows 真实 Stop 的已知验收版本，不把它误作最低兼容要求。"""
    match = re.search(r"codex-cli\s+(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        relation = "unknown"
    elif tuple(map(int, match.groups())) < (0, 155, 0):
        relation = "older_than_tested"
    else:
        relation = "same_or_newer"
    return {"windows_stop_tested_version": "0.155.0", "version_relation": relation}


def doctor(binary, root, config, thread_id=None):
    version = subprocess.run([binary, "--version"], capture_output=True, encoding="utf-8", timeout=10, **process_options())
    if version.returncode or not version.stdout.strip():
        raise BackendError("Codex CLI --version 失败；请检查 codex_bin 路径")
    output = {"codex_bin": binary, "version": version.stdout.strip(), "config": config,
              "data_dir": str(root), "desktop_display": "not_verified"}
    if sys.platform == "win32":
        output["cli_preflight"] = windows_cli_preflight(output["version"])
    # 仅检查定义，不创建或恢复任何会话，因此不会触发 SessionStart/Stop。
    with CodexBackend(binary, disable_hooks=False) as backend:
        cwd = str(Path.cwd())
        if thread_id:
            thread = backend.read(valid_id(thread_id))
            cwd = thread.get("cwd") or cwd
            snap = snapshot(thread, config)
            output["thread"] = {k: snap[k] for k in ("title", "latest_id", "has_messages")}
        output["app_server"] = "ok"
        try:
            listing = backend.call("hooks/list", {"cwds": [cwd]})
            definitions = [hook for entry in listing.get("data", []) for hook in entry.get("hooks", [])
                           if (hook.get("pluginId") or "").split("@")[0] == "oil-codex-title"]
            ready = any(h.get("enabled") and h.get("trustStatus") in ("trusted", "managed")
                        for h in definitions)
            output["hook"] = {
                "status": "ready" if ready else "needs_trust_or_enable" if definitions else "not_loaded",
                "definitions": [{k: h.get(k) for k in ("eventName", "enabled", "trustStatus", "sourcePath")}
                                for h in definitions],
            }
        except BackendError:
            output["hook"] = {"status": "inspection_unsupported"}
    return output


def main():
    # Hook 事件使用 UTF-8；不能依赖 Windows 当前代码页解释中文内容。
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="独立模型驱动的 Codex 话题命名")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook", help="读取 Stop Hook stdin；保持宿主输出为空 JSON")
    p = sub.add_parser("doctor", help="只读检查运行环境")
    p.add_argument("--thread")
    sub.add_parser("status", help="显示配置和本地记录数量")
    sub.add_parser("usage", help="汇总新版独立模型用量；缓存包含在输入中")
    sub.add_parser("pause", help="暂停自动命名")
    sub.add_parser("resume", help="恢复自动命名")
    p = sub.add_parser("configure", help="配置独立命名模型或兼容的可执行文件")
    p.add_argument("--model")
    p.add_argument("--codex-bin")
    p.add_argument("--service-tier", choices=("standard", "fast"))
    for name in ("rename", "lock", "unlock"):
        p = sub.add_parser(name)
        p.add_argument("thread_id")
        if name == "rename":
            p.add_argument("--apply", action="store_true", help="写入；省略时只预览")
    args = parser.parse_args()
    root = data_dir()
    is_hook = args.command == "hook"
    thread_id = None
    try:
        if sys.version_info < (3, 10):
            raise BackendError("需要 Python 3.10 或更新版本")
        config = load_config(root)
        if is_hook:
            if os.environ.get("OIL_CODEX_TITLE_WORKER") == "1" or not config["enabled"]:
                return 0
            event = json.loads(sys.stdin.read(1024 * 1024))
            if event.get("hook_event_name") != "Stop" or event.get("stop_hook_active"):
                return 0
            thread_id = valid_id(event["session_id"])
            turn_id = valid_id(event["turn_id"])
        if args.command in ("pause", "resume", "configure"):
            config_path = root / "config.json"
            changes = read_json(config_path)
            if args.command in ("pause", "resume"):
                changes["enabled"] = args.command == "resume"
            else:
                if args.model:
                    changes["model"] = args.model
                    # 新模型未必支持 Fast；切换模型时不继承旧模型的服务档位。
                    if args.model != config["model"] and not args.service_tier:
                        changes["service_tier"] = None
                if args.codex_bin:
                    changes["codex_bin"] = find_codex(args.codex_bin)
                if args.service_tier:
                    changes["service_tier"] = "priority" if args.service_tier == "fast" else None
            atomic_json(config_path, changes)
            print(json.dumps(load_config(root), ensure_ascii=False))
            return 0
        if args.command == "status":
            print(json.dumps({"config": config, "data_dir": str(root),
                              "tracked_threads": len(list((root / "threads").glob("*.json")))}, ensure_ascii=False))
            return 0
        if args.command == "usage":
            print(json.dumps(usage_report(root), ensure_ascii=False))
            return 0
        binary = find_codex(config["codex_bin"])
        if args.command == "doctor":
            result = doctor(binary, root, config, args.thread)
        else:
            thread_id = thread_id or valid_id(args.thread_id)
            with CodexBackend(binary) as backend:
                if args.command in ("lock", "unlock"):
                    with thread_lock(root, thread_id) as acquired:
                        if not acquired:
                            raise BackendError("该话题正在命名，稍后再试")
                        path = state_path(root, thread_id)
                        state = read_json(path)
                        state.update(locked=args.command == "lock", lock_reason="用户设置",
                                     last_seen_title=backend.read(thread_id).get("name") or "")
                        state.pop("last_fingerprint", None)
                        state.pop("pending_title", None)
                        atomic_json(path, state)
                        result = {"status": args.command, "title": state["last_seen_title"]}
                else:
                    result = process_thread(
                        backend, lambda context: limited_title(binary, root, config, context,
                            before_model=lambda: ensure_title_active(backend, thread_id, root)),
                        thread_id, root, config, apply=is_hook or args.apply,
                        event_turn=turn_id if is_hook else None,
                    )
                    if is_hook and result["status"] not in ("renamed", "kept"):
                        audit(root, thread_id, result)
        if not is_hook:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        error = {"status": "error", "error_type": type(exc).__name__}
        if is_hook:
            try:
                audit(root, thread_id or "hook", error)
            except Exception:
                pass
            return 0
        message = str(exc) if isinstance(exc, (BackendError, ValueError)) else "操作失败；检查输入及本地配置"
        print(json.dumps({**error, "message": message}, ensure_ascii=False))
        return 1
    finally:
        if is_hook:
            print("{}")


if __name__ == "__main__":
    raise SystemExit(main())
