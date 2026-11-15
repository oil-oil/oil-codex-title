"""测试跨进程命名中的写入边界、保护规则和 Hook 输出契约。"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import oil_codex_title as title
from codex_adapter import BackendError, worker_env, generate_title, normalize_project_prefix

ID = "12345678-1234-1234-1234-123456789012"
TURN = "12345678-1234-1234-1234-123456789013"
NEW_TURN = "12345678-1234-1234-1234-123456789014"


def thread():
    return {"name": "讨论事情", "turns": [{"id": TURN, "status": "completed", "items": [
        {"type": "userMessage", "content": [{"type": "text", "text": "请为产品设计视频讲解大纲"}]},
        {"type": "agentMessage", "phase": "final_answer", "text": "视频大纲已整理"},
        {"type": "commandExecution", "aggregatedOutput": "不应交给模型的工具输出"},
    ]}]}


class FakeBackend:
    def __init__(self):
        self.thread = thread()
        self.writes = []
        self.raise_after_write = False
        self.archived = False

    def is_archived(self, thread_id, cwd=None):
        return self.archived

    def read(self, thread_id):
        return copy.deepcopy(self.thread)

    def rename(self, thread_id, text):
        self.writes.append((thread_id, text))
        self.thread["name"] = text
        if self.raise_after_write:
            raise BackendError("写入后的连接中断")


def proposal(_):
    return {"action": "rename", "title": "🎬 产品视频｜讲解大纲", "reason": "主要目标已明确"}, {"input_tokens": 100}


class TitleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backend = FakeBackend()
        self.config = title.DEFAULTS.copy()

    def tearDown(self):
        self.tmp.cleanup()

    def process(self, generator=proposal, **kwargs):
        return title.process_thread(self.backend, generator, ID, self.root, self.config, **kwargs)

    def test_language_titles_survive_validation_write_and_readback(self):
        for new_title in (
            "🧩 Email verification｜Fix expiry",
            "🎨 ログイン画面｜余白調整",
            "🧩 Verificación｜Corregir caducidad",
            "🧩 邮箱验证码｜过期修复",
        ):
            with self.subTest(title=new_title):
                self.backend.thread["name"] = "待整理"
                title.state_path(self.root, ID).unlink(missing_ok=True)
                candidate = {"action": "rename", "title": new_title, "reason": "语言迁移"}
                result = self.process(generator=lambda _: (candidate, {}), apply=True)
                self.assertEqual(result["status"], "renamed")
                self.assertEqual(self.backend.read(ID)["name"], new_title)

    def test_preview_never_changes_title_or_history(self):
        before = copy.deepcopy(self.backend.thread)
        self.assertEqual(self.process()["status"], "preview")
        self.assertEqual(self.backend.thread, before)
        self.assertFalse(title.state_path(self.root, ID).exists())

    def test_archived_thread_skips_model_even_for_old_hook(self):
        self.backend.archived = True
        self.assertEqual(self.process(lambda _: self.fail("归档话题不能调用模型"),
                                      apply=True, event_turn=TURN)["status"], "archived")
        self.assertEqual(self.backend.writes, [])

    def test_archive_during_generation_discards_title(self):
        def moved(context):
            self.backend.archived = True
            return proposal(context)
        self.assertEqual(self.process(moved, apply=True)["status"], "archived")
        self.assertEqual(self.backend.writes, [])

    def test_archive_or_pause_prevents_collision_retry(self):
        from unittest.mock import Mock
        for change in ("archive", "pause"):
            with self.subTest(change=change):
                self.backend.archived = False
                title.atomic_json(self.root / "config.json", {"enabled": True})
                title.atomic_json(title.state_path(self.root, "12345678-1234-1234-1234-123456789099"),
                                  {"scope_key": "", "last_seen_title": proposal({})[0]["title"]})
                def moved(context):
                    if change == "archive":
                        self.backend.archived = True
                    else:
                        title.atomic_json(self.root / "config.json", {"enabled": False})
                    return proposal(context)
                model = Mock(side_effect=moved)
                result = self.process(model, apply=True)
                self.assertEqual(result["status"], "archived" if change == "archive" else "disabled")
                self.assertEqual(model.call_count, 1)
                self.assertEqual(self.backend.writes, [])

    def test_archive_or_pause_prevents_internal_format_retry(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        for change in ("archive", "pause"):
            with self.subTest(change=change):
                self.backend.archived = False
                title.atomic_json(self.root / "config.json", {"enabled": True})
                def fake_run(args, **kwargs):
                    output = Path(args[args.index("--output-last-message") + 1])
                    output.write_text(json.dumps({"action": "keep", "title": "旧标题", "reason": "格式合规"}), encoding="utf-8")
                    if change == "archive":
                        self.backend.archived = True
                    else:
                        title.atomic_json(self.root / "config.json", {"enabled": False})
                    return SimpleNamespace(returncode=0, stdout=json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}))
                def model(context):
                    return title.limited_title("unused", self.root, self.config, context,
                        before_model=lambda: title.ensure_title_active(self.backend, ID, self.root))
                with patch("codex_adapter.subprocess.run", side_effect=fake_run) as run:
                    result = self.process(model, apply=True)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(result["status"], "archived" if change == "archive" else "disabled")
                self.assertEqual(self.backend.writes, [])

    def test_archive_while_waiting_for_naming_slot_never_starts_model(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        @contextmanager
        def queued(*args):
            self.backend.archived = True
            yield True
        def model(context):
            return title.limited_title("unused", self.root, self.config, context,
                before_model=lambda: title.ensure_title_active(self.backend, ID, self.root))
        with patch.object(title, "worker_slot", queued), patch("codex_adapter.subprocess.run") as run:
            result = self.process(model, apply=True)
        run.assert_not_called()
        self.assertEqual(result["status"], "archived")

    def test_pause_during_slow_archive_probe_never_starts_model(self):
        from unittest.mock import patch
        def probe(*args):
            title.atomic_json(self.root / "config.json", {"enabled": False})
            return False
        with patch.object(self.backend, "is_archived", side_effect=probe), patch("codex_adapter.subprocess.run") as run:
            with self.assertRaises(title.ModelSkipped) as error:
                title.limited_title("unused", self.root, self.config, {"original_goal": "设计表单"},
                    before_model=lambda: title.ensure_title_active(self.backend, ID, self.root))
        self.assertEqual(error.exception.status, "disabled")
        run.assert_not_called()

    def test_apply_renames_only_metadata_and_deduplicates(self):
        turns = copy.deepcopy(self.backend.thread["turns"])
        self.assertEqual(self.process(apply=True)["status"], "renamed")
        self.assertEqual(self.backend.thread["turns"], turns)
        def forbidden(_):
            self.fail("重复内容不应调用模型")
        self.assertEqual(self.process(forbidden, apply=True)["status"], "unchanged")
        self.assertEqual(len(self.backend.writes), 1)

    def test_external_manual_title_is_locked(self):
        self.process(apply=True)
        self.backend.thread["name"] = "我的固定标题"
        result = self.process(apply=True)
        self.assertEqual(result["status"], "manual_title")
        self.assertTrue(title.read_json(title.state_path(self.root, ID))["locked"])
        self.assertEqual(self.backend.thread["name"], "我的固定标题")

    def test_explicit_lock_skips_model(self):
        title.atomic_json(title.state_path(self.root, ID), {"locked": True})
        self.assertEqual(self.process(lambda _: self.fail(), apply=True)["status"], "locked")

    def test_new_turn_during_model_discards_result(self):
        def moved(context):
            self.backend.thread["turns"].append({"id": NEW_TURN, "status": "completed", "items": []})
            return proposal(context)
        self.assertEqual(self.process(moved, apply=True)["status"], "stale_result")
        self.assertEqual(self.backend.writes, [])

    def test_first_run_host_title_change_does_not_create_manual_lock(self):
        def moved(context):
            self.backend.thread["name"] = "宿主生成的首轮标题"
            return proposal(context)
        self.assertEqual(self.process(moved, apply=True)["status"], "stale_result")
        self.assertEqual(self.process(apply=True)["status"], "renamed")
        self.assertFalse(title.read_json(title.state_path(self.root, ID)).get("locked"))

    def test_greeting_then_request_and_delayed_host_title(self):
        self.backend.thread["name"] = ""
        self.backend.thread["turns"][0]["items"] = [
            {"type": "userMessage", "content": [{"type": "text", "text": "你好"}]},
            {"type": "agentMessage", "phase": "final_answer", "text": "你好"},
        ]
        def next_turn(context):
            self.backend.thread["name"] = "回应中文问候"
            self.backend.thread["turns"].append({"id": NEW_TURN, "status": "completed", "items": [
                {"type": "userMessage", "content": [{"type": "text", "text": "查看本地 Skill"}]}
            ]})
            return proposal(context)
        self.assertEqual(self.process(next_turn, apply=True, event_turn=TURN)["status"], "stale_result")
        self.assertEqual(self.process(apply=True, event_turn=NEW_TURN)["status"], "renamed")

    def test_recovers_only_legacy_unestablished_automatic_lock(self):
        path = title.state_path(self.root, ID)
        title.atomic_json(path, {"last_seen_title": "讨论事情", "locked": True,
                                 "lock_reason": "检测到外部改名"})
        self.assertEqual(self.process(apply=True)["status"], "renamed")
        state = title.read_json(path)
        state.update(locked=True, lock_reason="检测到外部改名")
        title.atomic_json(path, state)
        self.assertEqual(self.process(lambda _: self.fail(), apply=True)["status"], "locked")

    def test_explicit_initial_lock_is_not_migrated(self):
        title.atomic_json(title.state_path(self.root, ID), {"last_seen_title": "讨论事情",
                          "locked": True, "lock_reason": "用户设置"})
        self.assertEqual(self.process(lambda _: self.fail(), apply=True)["status"], "locked")

    def test_stale_hook_does_not_call_model(self):
        self.assertEqual(self.process(lambda _: self.fail(), apply=True, event_turn=NEW_TURN)["status"], "outdated_event")

    def test_pause_during_generation_prevents_write(self):
        def paused(context):
            title.atomic_json(self.root / "config.json", {"enabled": False})
            return proposal(context)
        self.assertEqual(self.process(paused, apply=True)["status"], "disabled")
        self.assertEqual(self.backend.writes, [])

    def test_bad_model_output_never_writes(self):
        for bad in ("🎬 正常\n恶意换行", "没有 emoji", "🎬 标题 📝", "🎬 \u202e反向控制", "🎬 a@b.com"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.process(lambda _: ({"action": "rename", "title": bad, "reason": ""}, {}), apply=True)
        self.assertEqual(self.backend.writes, [])

    def test_keep_never_replaces_title(self):
        result = self.process(lambda _: ({"action": "keep", "title": "模型误改", "reason": "保持"}, {}), apply=True)
        self.assertEqual(result["status"], "kept")
        self.assertEqual(result["title"], "讨论事情")
        self.assertEqual(self.backend.writes, [])

    def test_lost_write_response_is_verified_without_retry(self):
        self.backend.raise_after_write = True
        self.assertEqual(self.process(apply=True)["status"], "renamed")
        self.assertEqual(len(self.backend.writes), 1)

    def test_nested_lock_skips_duplicate_worker(self):
        with title.thread_lock(self.root, ID):
            self.assertEqual(self.process(lambda _: self.fail(), apply=True)["status"], "busy")

    def test_new_hook_waits_for_old_worker_then_checks_latest_turn(self):
        result = []
        with title.thread_lock(self.root, ID):
            worker = threading.Thread(target=lambda: result.append(self.process(apply=True, event_turn=TURN)))
            worker.start()
            time.sleep(0.05)
            self.assertEqual(result, [])
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["status"], "renamed")

    def test_context_omits_ambient_state_tools_and_commentary(self):
        items = self.backend.thread["turns"][0]["items"]
        items[0]["content"][0]["text"] = '<in-app-browser-context>不可作为目标的网页</in-app-browser-context>实际请求'
        items.append({"type": "agentMessage", "phase": "commentary", "text": "临时安排"})
        context = title.snapshot(self.backend.thread, self.config)["context"]
        raw = json.dumps(context, ensure_ascii=False)
        self.assertIn("实际请求", raw)
        self.assertNotIn("不可作为目标", raw)
        self.assertNotIn("临时安排", raw)
        self.assertNotIn("不应交给模型", raw)

    def test_attachment_envelope_preserves_actual_request(self):
        raw = '<recommended_plugins>很长的插件目录</recommended_plugins>\n# Files mentioned by the user:\n文件说明\n## My request:\n修复 Maple 邮箱注册'
        self.assertEqual(title.clean_text(raw), '修复 Maple 邮箱注册')
        self.assertEqual(title.clean_text('<skill><name>示例技能</name>安装说明</skill>'), '')

    def test_project_hint_avoids_home_and_full_paths(self):
        self.assertEqual(title.project_hint({'cwd': '/workspace/maple'}), 'maple')
        self.assertEqual(title.project_hint({'cwd': str(Path.home())}), '')
        self.assertEqual(title.project_hint({'cwd': '/Users/example'}), '')
        self.assertEqual(title.project_hint({'cwd': '/workspace/projects'}), '')

    def test_duplicate_candidate_retries_with_evidence(self):
        title.atomic_json(title.state_path(self.root, NEW_TURN), {'last_seen_title': '🎬 产品视频｜讲解大纲'})
        contexts = []
        def generate(context):
            contexts.append(context)
            if len(contexts) == 1:
                return proposal(context)
            return {'action': 'rename', 'title': '🎬 Maple 产品入门视频｜大纲', 'reason': '补充产品名'}, {'input_tokens': 80}
        result = self.process(generate, apply=True)
        self.assertEqual(result['status'], 'renamed')
        self.assertEqual(result['usage']['input_tokens'], 180)
        self.assertIn('conflicting_titles', contexts[1])
        self.assertEqual(len(self.backend.writes), 1)

    def test_unresolved_duplicate_never_writes(self):
        title.atomic_json(title.state_path(self.root, NEW_TURN), {'last_seen_title': '🎬 产品视频｜讲解大纲'})
        self.assertEqual(self.process(apply=True)['status'], 'ambiguous_title')
        self.assertEqual(self.backend.writes, [])

    def test_same_title_in_different_project_is_allowed(self):
        self.backend.thread['cwd'] = '/workspace/maple'
        title.atomic_json(title.state_path(self.root, NEW_TURN), {
            'last_seen_title': '🎬 产品视频｜讲解大纲', 'scope_key': 'other-project'})
        calls = []
        def generate(context):
            calls.append(context)
            return proposal(context)
        self.assertEqual(self.process(generate, apply=True)['status'], 'renamed')
        self.assertEqual(len(calls), 1)
        state = title.read_json(title.state_path(self.root, ID))
        self.assertNotIn('/workspace/', json.dumps(state))
        self.assertEqual(len(state['scope_key']), 64)

    def test_arbitrary_second_emoji_is_rejected(self):
        with self.assertRaises(ValueError):
            title.validate_candidate({'action': 'rename', 'title': '🛠️ Maple 注册修复 🚀', 'reason': ''}, '')

    def test_failed_hook_returns_only_empty_json(self):
        env = os.environ | {"OIL_CODEX_TITLE_DATA": str(self.root)}
        env.pop("OIL_CODEX_TITLE_WORKER", None)
        for payload in ("not json", json.dumps({"hook_event_name": "Stop", "session_id": "../../escape"})):
            proc = subprocess.run([sys.executable, str(ROOT / "scripts/oil_codex_title.py"), "hook"],
                                  input=payload, capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "{}\n")
            self.assertEqual(proc.stderr, "")

    def test_worker_guard_does_not_start_recursive_model(self):
        env = os.environ | {"OIL_CODEX_TITLE_DATA": str(self.root), "OIL_CODEX_TITLE_WORKER": "1"}
        proc = subprocess.run([sys.executable, str(ROOT / "scripts/oil_codex_title.py"), "hook"],
                              input="ignored", capture_output=True, text=True, env=env)
        self.assertEqual(proc.stdout, "{}\n")
        self.assertFalse((self.root / "logs").exists())
        self.assertNotIn("CODEX_THREAD_ID", worker_env())

    def test_greeting_only_never_starts_model_process(self):
        candidate, usage = generate_title('/does-not-exist', self.config, {
            'current_title': '回应中文问候', 'original_goal': '你好！',
            'recent_turns': [{'messages': [{'role': 'user', 'text': '你好'}]}]}, ROOT)
        self.assertEqual(candidate['action'], 'keep')
        self.assertEqual(usage, {})

    def test_outer_project_prefix_normalizes_only_exact_identity(self):
        p = {'action': 'rename', 'title': '🎨 Kite LMS 课程详情页加载优化', 'reason': ''}
        self.assertEqual(normalize_project_prefix(p, {'project_hint':'kite-lms'})['title'], '🎨 课程详情页加载优化')
        self.assertEqual(normalize_project_prefix(p, {'project_hint':''}), p)

    def test_subproject_and_content_names_are_preserved(self):
        for hint, text in [('commerce-suite','🛠️ seller-console 订单导出修复'),
                           ('rednote','🔎 H3 与 H3 Max 模型评测'),
                           ('maple','🛠️ MaplePay 支付修复'),
                           ('maple','🛠️ Maple 与 Cedar 注册同步')]:
            p = {'action':'rename','title':text,'reason':''}
            self.assertEqual(normalize_project_prefix(p, {'project_hint':hint}), p)

    def test_kept_project_prefix_is_not_cleaned_up(self):
        p = {'action':'keep','title':'🛠️ Maple 注册修复','reason':''}
        self.assertEqual(normalize_project_prefix(p, {'project_hint':'maple'}), p)

    def test_structured_title_rejects_incomplete_or_multiple_parts(self):
        for bad in ("🧩 邮箱注册修复", "🧩 邮箱注册|修复", "🧩 ｜修复",
                    "🧩 邮箱注册｜", "🧩 邮箱注册｜修复｜测试", "🧩 邮箱注册 ｜修复",
                    "🛠️ 邮箱注册｜修复"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                title.validate_candidate({"action": "rename", "title": bad, "reason": ""}, "")

    def test_legacy_keep_is_allowed_but_new_tool_category_is_valid(self):
        old = "🛠️ 邮箱注册修复"
        result = title.validate_candidate({"action": "keep", "title": "", "reason": "信息不足"}, old)
        self.assertEqual(result["title"], old)
        new = "🧩 邮箱注册｜修复"
        self.assertEqual(title.validate_candidate({"action": "rename", "title": new, "reason": ""}, old)["title"], new)

    def test_policy_upgrade_rechecks_history_once_without_bypassing_locks(self):
        from unittest.mock import patch
        with patch.object(title, "POLICY_VERSION", title.POLICY_VERSION - 1):
            self.process(apply=True)
        calls = []
        def migrate(context):
            calls.append(context)
            return {"action": "keep", "title": context["current_title"], "reason": "准确"}, {}
        self.assertEqual(self.process(migrate, apply=True)["status"], "kept")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.process(migrate, apply=True)["status"], "unchanged")
        self.assertEqual(len(calls), 1)

    def test_legacy_keep_gets_one_bounded_format_review(self):
        from unittest.mock import patch
        context = {"current_title": "🔎 本地 Skill 清单梳理"}
        old = {"action": "keep", "title": context["current_title"], "reason": "结构合规"}
        new = {"action": "rename", "title": "📝 本地 Skill｜清单梳理", "reason": "格式迁移"}
        with patch("codex_adapter._generate_title_once", side_effect=[(old, {"input_tokens": 10}), (new, {"input_tokens": 20})]) as call:
            candidate, usage = generate_title("unused", {}, context, ROOT)
        self.assertEqual(candidate, new)
        self.assertEqual(usage["input_tokens"], 30)
        self.assertEqual(call.call_count, 2)
        self.assertIn("naming_feedback", call.call_args.args[2])

    def test_format_review_does_not_force_uncertain_keep_or_loop(self):
        from unittest.mock import patch
        keep = {"action": "keep", "title": "待定", "reason": "信息不足"}
        with patch("codex_adapter._generate_title_once", return_value=(keep, {"input_tokens": 10})) as call:
            candidate, _ = generate_title("unused", {}, {"current_title": "待定"}, ROOT)
        self.assertEqual(candidate, keep)
        self.assertEqual(call.call_count, 2)

    def test_structured_keep_needs_no_format_retry(self):
        from unittest.mock import patch
        keep = {"action": "keep", "title": "📝 页面还原｜方法整理", "reason": "主线准确"}
        with patch("codex_adapter._generate_title_once", return_value=(keep, {"input_tokens": 10})) as call:
            generate_title("unused", {}, {"current_title": keep["title"]}, ROOT)
        self.assertEqual(call.call_count, 1)

    def test_format_review_shares_model_deadline(self):
        from unittest.mock import patch
        keep = {"action": "keep", "title": "旧标题", "reason": ""}
        with patch("codex_adapter._generate_title_once", return_value=(keep, {"input_tokens": 10})) as call, patch("codex_adapter.time.monotonic", side_effect=[0, 90]):
            generate_title("unused", {"model_timeout_seconds": 100}, {"current_title": "旧标题"}, ROOT)
        self.assertEqual(call.call_args.args[1]["model_timeout_seconds"], 10)
        with patch("codex_adapter._generate_title_once", return_value=(keep, {"input_tokens": 10})) as call, patch("codex_adapter.time.monotonic", side_effect=[0, 101]):
            generate_title("unused", {"model_timeout_seconds": 100}, {"current_title": "旧标题"}, ROOT)
        self.assertEqual(call.call_count, 1)

    def test_global_worker_slots_limit_different_threads_and_release(self):
        with title.worker_slot(self.root, 2, 0) as first:
            with title.worker_slot(self.root, 2, 0) as second:
                with title.worker_slot(self.root, 2, 0) as third:
                    self.assertTrue(first)
                    self.assertTrue(second)
                    self.assertFalse(third)
            with title.worker_slot(self.root, 2, 0) as available:
                self.assertTrue(available)

    def test_global_queue_exhaustion_never_calls_model(self):
        from unittest.mock import patch
        config = {**self.config, "max_parallel_workers": 1, "model_timeout_seconds": 0}
        with title.worker_slot(self.root, 1, 0):
            with patch.object(title, "generate_title") as call, self.assertRaises(BackendError):
                title.limited_title("unused", self.root, config, {})
        call.assert_not_called()

    def test_stop_waits_for_completed_turn_instead_of_fixed_sleep(self):
        from unittest.mock import patch
        running = self.backend.read(ID)
        running["turns"][-1]["status"] = "inProgress"
        with patch.object(self.backend, "read", side_effect=[running, self.backend.thread]), patch.object(title.time, "sleep"):
            settled, status = title.read_settled_thread(self.backend, ID, TURN)
        self.assertIsNone(status)
        self.assertEqual(settled["turns"][-1]["status"], "completed")

    def test_unsettled_or_interrupted_turn_never_generates(self):
        from unittest.mock import patch
        self.backend.thread["turns"][-1]["status"] = "interrupted"
        self.assertEqual(self.process(lambda _:self.fail(), apply=True,event_turn=TURN)["status"], "unfinished_turn")
        self.backend.thread["turns"][-1]["status"] = "inProgress"
        with patch.object(title.time, "monotonic", side_effect=[0, 6]):
            _, status = title.read_settled_thread(self.backend, ID, TURN)
        self.assertEqual(status, "turn_not_settled")

    def test_config_merges_unknown_fields(self):
        title.atomic_json(self.root / "config.json", {"model": "custom-model", "future": "preserved"})
        config = title.load_config(self.root)
        self.assertEqual(config["future"], "preserved")
        self.assertEqual(config["recent_turns"], 5)

    def test_default_model_is_gpt_6_luna_fast(self):
        self.assertEqual(title.DEFAULTS["model"], "gpt-6-luna")
        self.assertEqual(title.DEFAULTS["service_tier"], "priority")

    def test_model_switch_does_not_inherit_unsupported_fast_tier(self):
        title.atomic_json(self.root / "config.json", {"model": "gpt-6-luna", "service_tier": "priority", "future": 1})
        env = os.environ | {"OIL_CODEX_TITLE_DATA": str(self.root)}
        proc = subprocess.run([sys.executable, str(ROOT / "scripts/oil_codex_title.py"),
                               "configure", "--model", "gpt-5.3-codex-spark"],
                              capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0)
        config = json.loads(proc.stdout)
        self.assertIsNone(config["service_tier"])
        self.assertEqual(config["future"], 1)

    def test_fast_option_maps_to_priority_service(self):
        env = os.environ | {"OIL_CODEX_TITLE_DATA": str(self.root)}
        proc = subprocess.run([sys.executable, str(ROOT / "scripts/oil_codex_title.py"),
                               "configure", "--model", "gpt-6-luna", "--service-tier", "fast"],
                              capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["service_tier"], "priority")


if __name__ == "__main__":
    unittest.main()
