"""沙箱移交队列、能力探测与 Hook 入口测试；不调用模型，也不启动 app-server。"""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import handoff
import oil_codex_title as app

ID = '12345678-1234-1234-1234-123456789012'
TURN = '87654321-4321-4321-4321-210987654321'
# 宿主传入的 transcrsipt 路径属于真实对话证据，不得进入移交请求。
HOOK_EVENT = json.dumps({
    "hook_event_name": "Stop",
    "session_id": ID,
    "turn_id": TURN,
    "transcript_path": "C:/Users/example/.codex/sessions/secret-rollout.jsonl",
})


class HandoffQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='handoff-test-')
        self.base = Path(self.tmp.name)
        self.queue = self.base / 'queue'

    def tearDown(self):
        self.tmp.cleanup()

    def test_writable_probe_rejects_unusable_directory(self):
        blocker = self.base / 'not-a-dir'
        blocker.write_text('x', encoding='utf-8')
        self.assertFalse(handoff.writable(blocker))
        self.assertFalse(handoff.state_writable(blocker))
        self.assertTrue(handoff.writable(self.base / 'state'))

    def test_submit_is_idempotent_and_leaves_no_partial_file(self):
        first = handoff.submit(self.queue, {"thread_id": ID, "turn_id": TURN})
        second = handoff.submit(self.queue, {"thread_id": ID, "turn_id": TURN, "attempts": 2})
        self.assertEqual(first, second)
        self.assertEqual(len(handoff.pending(self.queue)), 1)
        self.assertEqual(handoff.read_request(first)["attempts"], 2)
        self.assertEqual(list((self.queue / handoff.REQUESTS).glob('.submit-*')), [])

    def test_hand_off_keeps_only_identifiers(self):
        path = handoff.hand_off({"handoff_dir": str(self.queue)}, ID, TURN)
        text = path.read_text(encoding='utf-8')
        payload = json.loads(text)
        self.assertEqual((payload['thread_id'], payload['turn_id']), (ID, TURN))
        self.assertNotIn('secret-rollout.jsonl', text)
        self.assertTrue((self.queue / handoff.LOG_NAME).exists())

    def test_run_worker_archives_success_and_never_reprocesses(self):
        handoff.submit(self.queue, {"thread_id": ID, "turn_id": TURN})
        calls = []

        def process_one(payload):
            calls.append(payload['thread_id'])
            return {"status": "renamed"}

        stats = handoff.run_worker(self.queue, process_one)
        self.assertEqual(stats["renamed"], 1)
        self.assertEqual(calls, [ID])
        self.assertEqual(handoff.pending(self.queue), [])
        self.assertEqual(len(list((self.queue / handoff.DONE).glob('*.json'))), 1)
        # 已完成的请求不得再触发一次模型调用。
        self.assertEqual(handoff.run_worker(self.queue, process_one)["processed"], 0)
        self.assertEqual(len(calls), 1)

    def test_run_worker_retries_then_moves_to_failed(self):
        handoff.submit(self.queue, {"thread_id": ID, "turn_id": TURN})

        def boom(_payload):
            raise app.BackendError("App Server 连接已关闭")

        self.assertEqual(handoff.run_worker(self.queue, boom, max_attempts=2)["failed"], 1)
        remaining = handoff.pending(self.queue)
        self.assertEqual(len(remaining), 1)
        self.assertIn("App Server", handoff.read_request(remaining[0])["last_error"])
        handoff.run_worker(self.queue, boom, max_attempts=2)
        self.assertEqual(handoff.pending(self.queue), [])
        self.assertEqual(len(list((self.queue / handoff.FAILED).glob('*.json'))), 1)

    def test_run_worker_drops_request_without_thread_id(self):
        folder = self.queue / handoff.REQUESTS
        folder.mkdir(parents=True)
        (folder / 'broken.json').write_text('{"turn_id": null}', encoding='utf-8')
        stats = handoff.run_worker(self.queue, lambda _payload: self.fail('不应执行'))
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(handoff.pending(self.queue), [])

    def test_run_worker_isolates_one_failure_from_the_next_request(self):
        handoff.submit(self.queue, {"thread_id": ID, "turn_id": TURN})
        other = '11111111-2222-3333-4444-555555555555'
        handoff.submit(self.queue, {"thread_id": other, "turn_id": TURN})

        def process_one(payload):
            if payload['thread_id'] == ID:
                raise app.BackendError('坏请求')
            return {"status": "kept"}

        stats = handoff.run_worker(self.queue, process_one, max_attempts=2)
        self.assertEqual((stats['failed'], stats['kept']), (1, 1))
        self.assertEqual(len(handoff.pending(self.queue)), 1)


    def test_claim_hides_request_until_it_goes_stale(self):
        handoff.submit(self.queue, {"thread_id": ID, "turn_id": TURN})
        claimed = handoff._claim(handoff.pending(self.queue)[0])
        self.assertIsNotNone(claimed)
        self.assertTrue(claimed.name.endswith(".working"))
        self.assertEqual(handoff.pending(self.queue), [])
        self.assertEqual(handoff._reclaim_stale(self.queue, stale_seconds=300), [])
        old = time.time() - 600
        os.utime(claimed, (old, old))
        self.assertEqual(len(handoff._reclaim_stale(self.queue, stale_seconds=300)), 1)
        self.assertEqual(len(handoff.pending(self.queue)), 1)

    def test_worker_skips_request_already_claimed(self):
        handoff.submit(self.queue, {"thread_id": ID, "turn_id": TURN})
        handoff._claim(handoff.pending(self.queue)[0])  # 模拟另一个 worker 抢先
        calls = []

        def process_one(payload):
            calls.append(payload)
            return {"status": "renamed"}

        stats = handoff.run_worker(self.queue, process_one)
        self.assertEqual(stats["processed"], 0)
        self.assertEqual(calls, [])

    def test_spawn_worker_starts_detached_process_without_inheriting_pipes(self):
        with patch.object(handoff.subprocess, 'Popen') as popen:
            self.assertTrue(handoff.spawn_worker('script.py'))
        args, kwargs = popen.call_args
        self.assertEqual(args[0][0], handoff.sys.executable)
        self.assertIn('worker', args[0])
        self.assertEqual(kwargs['stdin'], handoff.subprocess.DEVNULL)
        self.assertEqual(kwargs['stdout'], handoff.subprocess.DEVNULL)
        self.assertTrue(kwargs['close_fds'])
        if os.name == 'nt':
            self.assertTrue(kwargs['creationflags'] & handoff.subprocess.DETACHED_PROCESS)

    def test_spawn_worker_failure_is_not_raised(self):
        with patch.object(handoff.subprocess, 'Popen', side_effect=OSError('nope')):
            self.assertFalse(handoff.spawn_worker('script.py'))


class HookHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hook-handoff-')
        self.base = Path(self.tmp.name)
        self.root = self.base / 'state'
        self.queue = self.base / 'queue'
        self.env = {"OIL_CODEX_TITLE_DATA": str(self.root),
                    "OIL_CODEX_TITLE_QUEUE": str(self.queue)}

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, command='hook'):
        stdout = io.StringIO()
        with patch.object(sys, 'argv', ['oil_codex_title.py', command]), \
                patch.object(sys, 'stdin', io.StringIO(HOOK_EVENT)), \
                patch.object(sys, 'stdout', stdout), \
                patch.dict(os.environ, self.env):
            code = app.main()
        return code, stdout.getvalue().strip()

    def test_hook_hands_off_and_spawns_worker_when_state_dir_is_not_writable(self):
        with patch.object(app.handoff, 'state_writable', return_value=False), \
                patch.object(app.handoff, 'spawn_worker', return_value=True) as spawn:
            code, out = self.run_cli()
        self.assertEqual((code, out), (0, '{}'))
        self.assertEqual(spawn.call_count, 1)
        requests = handoff.pending(self.queue)
        self.assertEqual(len(requests), 1)
        payload = handoff.read_request(requests[0])
        self.assertEqual(payload['thread_id'], ID)
        self.assertEqual(payload['hook_event_name'], 'Stop')

    def test_hook_always_hands_off_even_when_state_dir_is_writable(self):
        # 同步 Hook 必须秒回，因此命名一律交给后台 worker，不在 Hook 内联执行。
        with patch.object(app.handoff, 'state_writable', return_value=True), \
                patch.object(app.handoff, 'spawn_worker', return_value=True) as spawn:
            code, out = self.run_cli()
        self.assertEqual((code, out), (0, '{}'))
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(len(handoff.pending(self.queue)), 1)

    def test_hook_ignores_non_stop_events(self):
        stdout = io.StringIO()
        event = json.dumps({"hook_event_name": "PreToolUse", "session_id": ID, "turn_id": TURN})
        with patch.object(sys, 'argv', ['oil_codex_title.py', 'hook']), \
                patch.object(sys, 'stdin', io.StringIO(event)), \
                patch.object(sys, 'stdout', stdout), \
                patch.object(app.handoff, 'state_writable', return_value=False), \
                patch.dict(os.environ, self.env):
            code = app.main()
        self.assertEqual((code, stdout.getvalue().strip()), (0, '{}'))
        self.assertEqual(handoff.pending(self.queue), [])

    def test_worker_subcommand_reports_empty_queue(self):
        with patch('oil_codex_title.find_codex', return_value='codex'):
            code, out = self.run_cli('worker')
        self.assertEqual(code, 0)
        stats = json.loads(out)
        self.assertEqual(stats, {"processed": 0, "renamed": 0, "kept": 0, "skipped": 0, "failed": 0})


if __name__ == '__main__':
    unittest.main()
