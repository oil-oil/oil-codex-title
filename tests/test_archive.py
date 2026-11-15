"""归档生命周期与 Token 边界：普通测试不调用外部模型。"""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import archive_policy as archive
from codex_adapter import CodexBackend, BackendError
from oil_codex_title import atomic_json, read_json
from oil_codex_title import thread_lock

ID = "12345678-1234-1234-1234-123456789012"
TURN = "12345678-1234-1234-1234-123456789013"


class Backend:
    def __init__(self):
        self.archived = False
        self.reads = 0
        self.thread = {"id": ID, "name": "🎨 登录表单｜设计", "source": "vscode", "cwd": "/sample",
                       "status": {"type": "notLoaded"}, "updatedAt": time.time(), "turns": [
            {"id": TURN, "status": "completed", "completedAt": time.time() - 20 * archive.DAY,
             "items": [{"type": "userMessage", "content": [{"type": "text", "text": "设计登录表单"}]},
                       {"type": "agentMessage", "phase": "final_answer", "text": "登录表单已经交付"}]}]}

    def list_threads(self, *, archived=False, cwd=None):
        return iter([copy.deepcopy(self.thread)] if archived == self.archived else [])

    def read(self, tid):
        self.reads += 1
        return copy.deepcopy(self.thread)

    def is_archived(self, tid, cwd=None):
        return self.archived


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex")})
        self.env.start()
        self.backend = Backend()
        self.calls = 0
        self.host = {"pinnedThreads": [], "threads": [], "sections": []}
        archive.save_guards(self.root, self.host)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def model(self, context, **kwargs):
        self.calls += 1
        return {"classification": "completed", "reason": "明确交付，未发现待办"}, {"input_tokens": 100}

    def scan(self, model=None):
        return archive.scan(self.backend, self.root, model or self.model)

    def enable(self):
        atomic_json(self.root / "archive/config.json", {"enabled": True})

    def test_preview_only_and_cache_unchanged_content(self):
        before = copy.deepcopy(self.backend.thread)
        self.assertEqual(len(self.scan()["candidates"]), 1)
        self.assertEqual(len(self.scan()["candidates"]), 1)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.backend.thread, before)
        self.assertFalse(self.backend.archived)
        self.assertEqual(archive.check(self.backend, self.root, ID)["status"], "disabled")

    def test_archived_excluded_before_read_and_model(self):
        self.backend.archived = True
        self.assertEqual(self.scan()["counts"], {})
        self.assertEqual((self.backend.reads, self.calls), (0, 0))

    def test_terminal_record_protects_manual_restore(self):
        self.scan()
        self.backend.archived = True
        archive.mark_archived(self.backend, self.root, ID)
        self.backend.archived = False
        reads = self.backend.reads
        self.assertEqual(self.scan()["counts"]["terminal_or_protected"], 1)
        self.assertEqual((self.backend.reads, self.calls), (reads, 1))

    def test_no_success_record_without_host_archive(self):
        with self.assertRaises(ValueError):
            archive.mark_archived(self.backend, self.root, ID)

    def test_terminal_record_does_not_race_with_scan(self):
        self.backend.archived = True
        with thread_lock(self.root / "archive", "scan"):
            with self.assertRaises(ValueError):
                archive.mark_archived(self.backend, self.root, ID)
        self.assertFalse(archive.record_path(self.root, ID).exists())

    def test_failed_scan_leaves_no_repeated_model_attempt(self):
        def interrupted(context, **kwargs):
            self.calls += 1
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.scan(interrupted)
        self.scan()
        self.assertEqual(self.calls, 1)

    def test_title_updates_do_not_restart_inactivity_or_model(self):
        self.scan()
        self.backend.thread.update(name="用户重命名", updatedAt=time.time())
        self.assertEqual(len(self.scan()["candidates"]), 1)
        self.assertEqual(self.calls, 1)

    def test_new_conversation_invalidates_cache(self):
        self.scan()
        self.backend.thread["turns"][0]["items"][0]["content"][0]["text"] += "和注册表单"
        self.scan()
        self.assertEqual(self.calls, 2)

    def test_recent_or_missing_timestamp_skips_model(self):
        for stamp in (None, time.time()):
            self.backend.thread["turns"][0]["completedAt"] = stamp
            self.assertFalse(self.scan()["candidates"])
        self.assertEqual(self.calls, 0)

    def test_incomplete_turn_or_partial_items_skips_model(self):
        self.backend.thread["turns"][0]["status"] = "inProgress"
        self.scan()
        self.backend.thread["turns"][0].update(status="completed", itemsView="summary")
        self.scan()
        self.assertEqual(self.calls, 0)

    def test_pinned_and_running_and_manually_protected_skip(self):
        self.host["pinnedThreads"] = [{"id": ID, "hostId": "local", "kind": "codex", "status": "idle"}]
        archive.save_guards(self.root, self.host)
        self.scan()
        archive.save_guards(self.root, {"pinnedThreads": [], "threads": []})
        self.backend.thread["status"] = {"type": "active"}
        self.scan()
        self.backend.thread["status"] = {"type": "notLoaded"}
        atomic_json(archive.record_path(self.root, ID), {"protected": True})
        self.scan()
        self.assertEqual(self.calls, 0)

    def test_stale_or_incomplete_guards_fail_closed(self):
        atomic_json(self.root / "archive/guards.json", {"created_at": time.time() - 301})
        with self.assertRaises(ValueError):
            self.scan()
        with self.assertRaises(ValueError):
            archive.save_guards(self.root, {**self.host, "unavailableHosts": ["local"]})
        self.assertEqual(self.calls, 0)

    def test_active_automation_protected_and_unknown_scope_blocks(self):
        path = self.root / "codex/automations/example/automation.toml"
        path.parent.mkdir(parents=True)
        path.write_text('status = "ACTIVE"\ntarget_thread_id = "' + ID + '"\n', encoding="utf-8")
        self.assertFalse(self.scan()["candidates"])
        self.assertEqual(self.calls, 0)
        path.write_text('status = "ACTIVE"\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.scan()

    def test_open_and_uncertain_are_not_reassessed_daily(self):
        for category in ("open", "uncertain"):
            atomic_json(archive.record_path(self.root, ID), {})
            def model(context, **kwargs):
                self.calls += 1
                return {"classification": category, "reason": "需要保留"}, {}
            self.assertFalse(self.scan(model)["candidates"])
            self.assertFalse(self.scan(model)["candidates"])
        self.assertEqual(self.calls, 2)

    def test_error_does_not_cause_paid_retry_on_next_run(self):
        def failed(context, **kwargs):
            self.calls += 1
            raise BackendError("模型失败")
        self.assertEqual(self.scan(failed)["counts"]["errors"], 1)
        self.scan(failed)
        self.assertEqual(self.calls, 1)

    def test_threshold_crossing_reuses_same_classification(self):
        def model(context, **kwargs):
            self.calls += 1
            return {"classification": "no_pending", "reason": "问答已回应"}, {}
        self.assertFalse(self.scan(model)["candidates"])
        with patch.object(archive.time, "time", return_value=time.time() + 11 * archive.DAY):
            archive.save_guards(self.root, self.host)
            self.assertEqual(len(self.scan(model)["candidates"]), 1)
        self.assertEqual(self.calls, 1)

    def test_deterministic_scan_has_no_model(self):
        result = archive.scan(self.backend, self.root)
        self.assertEqual(result["counts"]["awaiting_evaluation"], 1)
        self.assertFalse(archive.record_path(self.root, ID).exists())

    def test_model_limit_zero_never_calls(self):
        archive.scan(self.backend, self.root, self.model, max_evaluations=0)
        self.assertEqual(self.calls, 0)

    def test_write_preflight_rechecks_new_content_and_new_archive(self):
        self.scan()
        self.enable()
        self.assertEqual(archive.check(self.backend, self.root, ID)["status"], "ready_to_archive")
        self.backend.thread["turns"][0]["items"].append({"type": "agentMessage", "text": "还需修改"})
        self.assertEqual(archive.check(self.backend, self.root, ID)["status"], "not_eligible")
        self.backend.archived = True
        self.assertEqual(archive.check(self.backend, self.root, ID)["status"], "protected_or_archived")

    def test_archived_during_model_not_added_to_candidates(self):
        def model(context, **kwargs):
            self.backend.archived = True
            return self.model(context)
        self.assertEqual(self.scan(model)["counts"]["stale"], 1)

    def test_archive_lists_paginate_and_deduplicate(self):
        b = CodexBackend("unused")
        calls = []
        def call(method, params):
            calls.append(dict(params))
            return {"data": [{"id": ID}], "nextCursor": None if params.get("cursor") else "page-2"}
        b.call = call
        self.assertEqual(len(list(b.list_threads(archived=True))), 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c["archived"] for c in calls))

    def test_pending_work_in_middle_and_tail_is_not_truncated(self):
        text = "界面已交付。" + "检查通过。" * 110 + "待办：实现邮件发送。" + "布局说明。" * 100 + "待办：上线验收。"
        self.backend.thread["turns"][0]["items"][-1]["text"] = text
        def model(context, **kwargs):
            self.assertEqual(context["recent_turns"][-1]["messages"][-1]["text"], text)
            return {"classification": "open", "reason": "仍有明确待办"}, {}
        self.assertFalse(self.scan(model)["candidates"])

    def test_long_user_request_is_complete(self):
        text = "请实施功能。" * 250 + "最后还需要完成部署。"
        self.backend.thread["turns"][0]["items"][0]["content"][0]["text"] = text
        context = archive.activity(self.backend.thread)[2]
        self.assertEqual(context["original_goal"], text)
        self.assertEqual(context["recent_turns"][0]["messages"][0]["text"], text)

    def test_oversized_context_is_kept_without_model(self):
        self.backend.thread["turns"][0]["items"][-1]["text"] = "说明" * archive.MAX_CONTEXT_CHARS
        self.assertFalse(self.scan()["candidates"])
        self.assertEqual(self.calls, 0)

    def test_legacy_positive_cache_cannot_archive_or_charge_again(self):
        self.scan()
        path = archive.record_path(self.root, ID)
        state = read_json(path)
        state.pop("context_version")
        atomic_json(path, state)
        self.enable()
        result = self.scan()
        self.assertEqual(result["counts"]["legacy_kept"], 1)
        self.assertFalse(result["candidates"])
        self.assertEqual(self.calls, 1)
        self.assertEqual(archive.check(self.backend, self.root, ID)["status"], "not_eligible")

    def test_scheduled_scan_disabled_never_lists_or_calls(self):
        with patch.object(self.backend, "list_threads") as listing:
            result = archive.scan(self.backend, self.root, self.model, scheduled=True)
        listing.assert_not_called()
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(self.calls, 0)

    def test_pause_during_first_evaluation_stops_remaining_candidates(self):
        self.enable()
        second = copy.deepcopy(self.backend.thread)
        second["id"] = "12345678-1234-1234-1234-123456789099"
        rows = {ID: self.backend.thread, second["id"]: second}
        self.backend.list_threads = lambda **kw: iter(copy.deepcopy(list(rows.values())))
        self.backend.read = lambda tid: copy.deepcopy(rows[tid])
        def model(context, **kwargs):
            atomic_json(self.root / "archive/config.json", {"enabled": False})
            return self.model(context)
        result = self.scan(model)
        self.assertEqual(self.calls, 1)
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(result["enabled"])
        self.assertFalse(result["candidates"])

    def test_explicit_pause_also_stops_manual_preview(self):
        def model(context, **kwargs):
            atomic_json(self.root / "archive/config.json", {"enabled": False, "pause_revision": 1})
            return self.model(context)
        self.assertEqual(self.scan(model)["status"], "disabled")

    def test_pause_or_archive_while_waiting_for_slot_never_starts_model(self):
        from contextlib import contextmanager
        from oil_codex_title import DEFAULTS
        for change in ("pause", "archive"):
            with self.subTest(change=change):
                self.enable()
                self.backend.archived = False
                archive.record_path(self.root, ID).unlink(missing_ok=True)
                @contextmanager
                def queued(*args):
                    if change == "pause":
                        atomic_json(self.root / "archive/config.json", {"enabled": False})
                    else:
                        self.backend.archived = True
                    yield True
                def model(context, **kwargs):
                    return archive.classify("unused", self.root, DEFAULTS, context, **kwargs)
                with patch.object(archive, "worker_slot", queued), patch("codex_adapter.subprocess.run") as run:
                    result = self.scan(model)
                run.assert_not_called()
                self.assertFalse(result["candidates"])
                self.assertFalse(archive.record_path(self.root, ID).exists())



if __name__ == "__main__":
    unittest.main()
