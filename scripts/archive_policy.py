"""归档候选、去重与写入前复核；真正归档只交给 Codex 宿主工具。"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

from usage_ledger import current_scope, usage_scope
from codex_adapter import BackendError, ModelSkipped, CodexBackend, find_codex, generate_json
from oil_codex_title import (ROOT, atomic_json, data_dir, load_config, read_json,
                             item_text, project_hint, state_path, thread_lock, valid_id, worker_slot)

DAY = 86400
CONTEXT_VERSION = 2
MAX_CONTEXT_CHARS = 12000
DEFAULT_BUDGET_SECONDS = 150
MIN_MODEL_WINDOW_SECONDS = 20
POLICY = {"enabled": False, "completed_days": 14, "inactive_days": 30, "protected_ids": []}
SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "classification": {"type": "string", "enum": ["completed", "no_pending", "open", "uncertain"]},
    "reason": {"type": "string"}}, "required": ["classification", "reason"]}


def policy(root):
    value = POLICY | read_json(root / "archive/config.json")
    if type(value["enabled"]) is not bool:
        raise ValueError("归档开关必须是布尔值")
    if not all(type(value[k]) is int for k in ("completed_days", "inactive_days")) or not (
        1 <= value["completed_days"] <= value["inactive_days"] <= 365
    ):
        raise ValueError("归档天数必须满足 1 ≤ 已完成 ≤ 无待办 ≤ 365")
    value["protected_ids"] = [valid_id(i) for i in value["protected_ids"]]
    return value


def record_path(root, tid):
    return root / "archive/threads" / (valid_id(tid) + ".json")


def automation_protection():
    """只读宿主管理的自动任务。无法识别活动任务的作用域时停止，避免漏保护。"""
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    ids, projects = set(), set()
    for path in (home / "automations").glob("*/automation.toml"):
        raw = path.read_text(encoding="utf-8")
        # 宿主生成的顶层简单字符串；不解析、不执行 prompt 或任意 TOML 内容。
        def field(key):
            match = re.search(r'^' + re.escape(key) + r'\s*=\s*("[^"\n]*")\s*$', raw, re.M)
            return json.loads(match[1]) if match else None
        status = field("status")
        if status in ("PAUSED", "COMPLETED"):
            continue
        if status != "ACTIVE":
            raise ValueError("无法识别自动任务状态，停止归档扫描")
        tid, project = field("target_thread_id"), field("project_id")
        if tid:
            ids.add(valid_id(tid))
        elif project:
            projects.add(project)
        else:
            raise ValueError("活动自动任务缺少可验证作用域，停止归档扫描")
    return ids, projects


def save_guards(root, host, current_id=None):
    """宿主 list_threads 的新鲜快照；置顶列表始终完整，不依赖普通列表分页。"""
    if not isinstance(host, dict) or not isinstance(host.get("pinnedThreads"), list) or not isinstance(host.get("threads"), list):
        raise ValueError("需要宿主 list_threads 的完整 JSON")
    if host.get("unavailableHosts") or host.get("unavailableSources"):
        raise ValueError("宿主列表不完整，停止归档扫描")
    protected, projects = automation_protection()
    for item in host["pinnedThreads"] + host["threads"]:
        if item.get("hostId") != "local" or item.get("kind") != "codex":
            continue
        if item in host["pinnedThreads"] or item.get("status") not in ("idle", "notLoaded"):
            protected.add(valid_id(item["id"]))
    # 用户放进自定义分区的单独话题视为主动整理，保守保留。
    for section in host.get("sections", []):
        if section.get("sectionId") in ("pinned", "threads", "chats"):
            continue
        for key in section.get("itemKeys", []):
            if key.startswith("codex:thread:local:"):
                protected.add(valid_id(key.split(":")[-1]))
    if current_id:
        protected.add(valid_id(current_id))
    value = {"created_at": time.time(), "protected_ids": sorted(protected),
             "protected_projects": sorted(projects)}
    atomic_json(root / "archive/guards.json", value)
    return {"status": "guards_saved", "protected_count": len(protected)}


def guards(root, now):
    value = read_json(root / "archive/guards.json")
    age = now - value.get("created_at", 0)
    if not 0 <= age <= 300:
        raise ValueError("保护名单缺失或超过 5 分钟；先重新读取宿主 list_threads 并保存 guards")
    # 在每次扫描和执行前再次检查自动任务，不能只信任之前的名单。
    ids, projects = automation_protection()
    return set(value["protected_ids"]) | ids, set(value["protected_projects"]) | projects


def archive_context(thread):
    """归档专用完整文本；超过预算就保留，不能遗漏中间或末尾的待办。"""
    effective, original = [], ""
    turns = thread.get("turns", [])
    for index, turn in enumerate(turns):
        messages = []
        for item in turn.get("items", []):
            kind = item.get("type")
            if kind != "userMessage" and not (
                kind == "agentMessage" and item.get("phase") in (None, "final_answer")
            ):
                continue
            text = item_text(item)
            if text:
                role = "user" if kind == "userMessage" else "assistant"
                messages.append({"role": role, "text": text})
                if role == "user" and not original:
                    original = text
        if messages and index >= len(turns) - 5:
            effective.append({"id": turn["id"], "messages": messages})
    if not original or not effective:
        return None
    context = {"current_title": thread.get("name") or "", "project_hint": project_hint(thread),
               "original_goal": original, "recent_turns": effective[-5:]}
    if len(json.dumps(context, ensure_ascii=False)) > MAX_CONTEXT_CHARS:
        return None
    return context


def activity(thread):
    """使用真实轮次时间和内容版本；标题与元数据更新时间不影响闲置时钟。"""
    turns = thread.get("turns", [])
    if not turns or turns[-1].get("status") != "completed":
        return None
    if any(t.get("itemsView", "full") != "full" for t in turns[-5:]):
        return None
    latest = turns[-1]
    stamp = latest.get("completedAt") or latest.get("startedAt")
    if type(stamp) not in (float, int) or stamp <= 0:
        return None
    context = archive_context(thread)
    if context is None:
        return None
    # 包括未截断的最近轮次，避免内容末尾变化未进入摘要时错误复用。
    encoded = json.dumps({"turns": turns[-5:]}, sort_keys=True, ensure_ascii=False)
    return stamp, hashlib.sha256(encoded.encode()).hexdigest(), context


def protected(thread, tid, cfg, guard, root):
    ids, projects = guard
    if tid in ids or tid in cfg["protected_ids"] or thread.get("projectId") in projects:
        return True
    if thread.get("ephemeral") or thread.get("parentThreadId"):
        return True
    if thread.get("source") not in ("cli", "vscode", "appServer"):
        return True
    if thread.get("status", {}).get("type") not in ("idle", "notLoaded"):
        return True
    return bool(read_json(state_path(root, tid)).get("locked"))


def eligible(classification, age, cfg):
    return ((classification == "completed" and age >= cfg["completed_days"] * DAY)
            or (classification == "no_pending" and age >= cfg["inactive_days"] * DAY))


def classify(binary, root, config, context, *, before_model=None, timeout_seconds=None):
    scope = current_scope()
    if scope is not None:
        scope.deferred_start = True
    timeout = config["model_timeout_seconds"]
    if timeout_seconds is not None:
        timeout = min(timeout, timeout_seconds)
    min_window = MIN_MODEL_WINDOW_SECONDS if config["model_timeout_seconds"] >= MIN_MODEL_WINDOW_SECONDS else 0
    config = {**config, "model_timeout_seconds": timeout}
    deadline = time.monotonic() + config["model_timeout_seconds"]
    with worker_slot(root, config["max_parallel_workers"], config["model_timeout_seconds"]) as acquired:
        remaining = deadline - time.monotonic()
        if not acquired or remaining <= 0:
            raise BackendError("模型并发已满，保留话题")
        return generate_json(binary, {**config, "model_timeout_seconds": remaining}, context,
                             ROOT / "prompts/archiving.md", SCHEMA, before_model=before_model,
                             min_remaining_seconds=min_window)


def scan(backend, root, classifier=None, max_evaluations=10, *, scheduled=False,
         budget_seconds=DEFAULT_BUDGET_SECONDS, min_model_window_seconds=MIN_MODEL_WINDOW_SECONDS):
    """预览也持久化评估缓存；同一内容版本只尝试一次模型，失败不自动重试。"""
    deadline = time.monotonic() + budget_seconds
    now = time.time()
    cfg = policy(root)
    if scheduled and not cfg["enabled"]:
        return {"status": "disabled", "enabled": False, "counts": {}, "candidates": []}
    guard = guards(root, now)
    def ensure_scan_active():
        fresh = policy(root)
        if ((scheduled or cfg["enabled"]) and not fresh["enabled"]
                or fresh.get("pause_revision") != cfg.get("pause_revision")):
            raise ModelSkipped("disabled")
    stopped = False
    counts, candidates = Counter(), []
    with thread_lock(root / "archive", "scan") as acquired:
        if not acquired:
            return {"status": "busy"}
        for meta in backend.list_threads(archived=False):
            if time.monotonic() >= deadline:
                counts["budget_exhausted"] += 1
                break
            try:
                ensure_scan_active()
            except ModelSkipped:
                stopped = True
                break
            tid = valid_id(meta["id"])
            counts["listed"] += 1
            state = read_json(record_path(root, tid))
            # 归档终态优先于任何内容读取或模型。手工恢复后仍保护，需显式 release。
            if state.get("archived_at") or state.get("protected"):
                counts["terminal_or_protected"] += 1
                continue
            if protected(meta, tid, cfg, guard, root):
                counts["protected"] += 1
                continue
            try:
                thread = backend.read(tid)
                if protected(thread, tid, cfg, guard, root):
                    counts["protected"] += 1
                    continue
                info = activity(thread)
                if info is None:
                    counts["unknown_activity"] += 1
                    continue
                stamp, fingerprint, context = info
                age = now - stamp
                if age < cfg["completed_days"] * DAY:
                    counts["recent"] += 1
                    continue
                if state.get("fingerprint") == fingerprint:
                    counts["cached"] += 1
                    if state.get("context_version") != CONTEXT_VERSION:
                        # 不复用旧截断摘要的正向结论，也不自动清缓存再次收费。
                        counts["legacy_kept"] += 1
                        continue
                else:
                    if classifier is None or counts["model_attempts"] >= max_evaluations:
                        counts["awaiting_evaluation"] += 1
                        continue
                    if deadline - time.monotonic() < min_model_window_seconds:
                        counts["budget_exhausted"] += 1
                        break
                    ensure_scan_active()
                    def before_model():
                        ensure_scan_active()
                        if deadline - time.monotonic() < min_model_window_seconds:
                            raise ModelSkipped("budget_exhausted")
                        if backend.is_archived(tid, thread.get("cwd")):
                            raise ModelSkipped("archived")
                        fresh = backend.read(tid)
                        fresh_info = activity(fresh)
                        fresh_cfg = policy(root)
                        if (protected(fresh, tid, fresh_cfg, guards(root, time.time()), root)
                                or not fresh_info or fresh_info[1] != fingerprint):
                            raise ModelSkipped("stale")
                        ensure_scan_active()
                    state = {"fingerprint": fingerprint, "context_version": CONTEXT_VERSION,
                             "classification": "uncertain",
                             "reason": "评估未完成，自动保留；需要时可显式重新评估", "evaluated_at": now}
                    started = False
                    def mark_started():
                        nonlocal started
                        if started:
                            return
                        started = True
                        # 先落盘再真正拉起模型，崩溃/异常不会导致下一次无界重试；队列等待或预算不足跳过时不污染缓存。
                        atomic_json(record_path(root, tid), state)
                        counts["model_attempts"] += 1
                    with usage_scope(root, "archiving", tid, on_begin=mark_started) as accounting:
                        try:
                            result, usage = classifier(context, before_model=before_model,
                                    timeout_seconds=max(0, deadline - time.monotonic()))
                        except ModelSkipped:
                            raise
                        except BaseException:
                            if not accounting.deferred_start:
                                mark_started()
                            raise
                        if not accounting.deferred_start:
                            mark_started()
                        ensure_scan_active()
                        if (not isinstance(result, dict) or set(result) != {"classification", "reason"}
                                or result["classification"] not in SCHEMA["properties"]["classification"]["enum"]
                                or not isinstance(result["reason"], str)):
                            raise ValueError("归档评估输出无效")
                        fresh = backend.read(tid)
                        fresh_info = activity(fresh)
                        if (protected(fresh, tid, cfg, guard, root) or not fresh_info
                                or fresh_info[1] != fingerprint or backend.is_archived(tid, fresh.get("cwd"))):
                            accounting.outcome = "stale"
                            counts["stale"] += 1
                            continue
                        state.update(classification=result["classification"], reason=result["reason"][:200], usage=usage)
                        atomic_json(record_path(root, tid), state)
                        accounting.outcome = "evaluated"
                if eligible(state.get("classification"), age, cfg):
                    candidates.append({"id": tid, "title": thread.get("name") or "", "fingerprint": fingerprint,
                                       "idle_days": int(age / DAY), "reason": state["reason"]})
                else:
                    counts["kept"] += 1
            except ModelSkipped as exc:
                if exc.status == "disabled":
                    stopped = True
                    break
                counts[exc.status] += 1
                if exc.status == "budget_exhausted":
                    break
            except (BackendError, ValueError, OSError):
                counts["errors"] += 1
        report = {"status": "disabled" if stopped else "preview", "created_at": now,
                  "enabled": policy(root)["enabled"],
                  "counts": dict(counts), "candidates": [] if stopped else candidates}
        atomic_json(root / "archive/preview.json", report)
        return report


def check(backend, root, tid):
    """每次宿主归档前调用；没有有效授权开关/缓存/新鲜保护名单时不放行。"""
    tid = valid_id(tid)
    now, cfg = time.time(), policy(root)
    if not cfg["enabled"]:
        return {"status": "disabled"}
    guard = guards(root, now)
    state = read_json(record_path(root, tid))
    if state.get("archived_at") or state.get("protected") or backend.is_archived(tid):
        return {"status": "protected_or_archived"}
    thread = backend.read(tid)
    info = activity(thread)
    if protected(thread, tid, cfg, guard, root) or info is None:
        return {"status": "protected"}
    stamp, fingerprint, _ = info
    if (state.get("context_version") != CONTEXT_VERSION or fingerprint != state.get("fingerprint")
            or not eligible(state.get("classification"), now - stamp, cfg)):
        return {"status": "not_eligible"}
    return {"status": "ready_to_archive", "id": tid, "title": thread.get("name") or ""}


def mark_archived(backend, root, tid):
    tid = valid_id(tid)
    with thread_lock(root / "archive", "scan") as acquired:
        if not acquired:
            raise ValueError("归档扫描正在运行，稍后再记录终态")
        if not backend.is_archived(tid):
            raise ValueError("宿主尚未确认归档，不能记录成功")
        path = record_path(root, tid)
        state = read_json(path)
        state.setdefault("archived_at", time.time())
        atomic_json(path, state)
    return {"status": "archived", "id": tid}


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="归档预览与去重；不直接修改宿主归档状态")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("guards", help="从 stdin 接收宿主 list_threads 的 JSON")
    p.add_argument("--current-id", default=os.environ.get("CODEX_THREAD_ID"))
    p = sub.add_parser("scan")
    p.add_argument("--live", action="store_true", help="允许独立模型评估新候选，消耗额度")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--budget-seconds", type=int, default=DEFAULT_BUDGET_SECONDS,
                   help="整轮扫描软预算（秒），范围 1～600")
    p.add_argument("--scheduled", action="store_true", help="定时入口；归档开关关闭时不评估")
    p.add_argument("--guards-stdin", action="store_true", help="先从 stdin 保存新鲜宿主名单，再在同一调用内扫描")
    p.add_argument("--current-id", default=os.environ.get("CODEX_THREAD_ID"))
    for name in ("check", "record", "protect", "release"):
        sub.add_parser(name).add_argument("thread_id")
    for name in ("enable", "pause", "status"):
        sub.add_parser(name)
    args = parser.parse_args()
    root = data_dir()
    try:
        if args.command == "guards":
            result = save_guards(root, json.load(sys.stdin), args.current_id)
        elif args.command in ("enable", "pause", "status"):
            cfg = policy(root)
            if args.command != "status":
                cfg["enabled"] = args.command == "enable"
                if args.command == "pause":
                    cfg["pause_revision"] = time.time_ns()
                atomic_json(root / "archive/config.json", cfg)
            result = {"config": cfg, "data_dir": str(root / "archive")}
        elif args.command in ("protect", "release"):
            path = record_path(root, args.thread_id)
            with thread_lock(root / "archive", "scan") as acquired:
                if not acquired:
                    raise ValueError("归档扫描正在运行，稍后再试")
                state = read_json(path)
                if args.command == "protect":
                    state["protected"] = True
                else:
                    state = {}  # 只解除本插件记录；不恢复、不启动原任务。
                atomic_json(path, state)
            result = {"status": args.command}
        elif args.command == "scan" and args.scheduled and not policy(root)["enabled"]:
            result = {"status": "disabled", "enabled": False, "counts": {}, "candidates": []}
        else:
            if args.command == "scan":
                if not 0 <= args.limit <= 20:
                    raise ValueError("单次模型评估数量必须在 0～20 之间")
                if not 1 <= args.budget_seconds <= 600:
                    raise ValueError("扫描时间预算必须在 1～600 秒之间")
                if args.guards_stdin:
                    save_guards(root, json.load(sys.stdin), args.current_id)
            config = load_config(root)
            binary = find_codex(config["codex_bin"])
            with CodexBackend(binary) as backend:
                if args.command == "scan":
                    fn = (lambda context, **kw: classify(binary, root, config, context, **kw)) if args.live else None
                    result = scan(backend, root, fn, args.limit, scheduled=args.scheduled,
                                  budget_seconds=args.budget_seconds)
                elif args.command == "check":
                    result = check(backend, root, args.thread_id)
                else:
                    result = mark_archived(backend, root, args.thread_id)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (BackendError, ValueError, OSError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
