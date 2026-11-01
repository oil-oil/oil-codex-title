"""队列和身份移交回归；不访问真实账号、注册任务或调用外部模型。"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import handoff
import oil_codex_title as app
from codex_adapter import diagnostic_category
from worker_identity import owner_process
from worker_setup import RUNNER

ID = '12345678-1234-1234-1234-123456789012'
TURN = '12345678-1234-1234-1234-123456789013'


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue = self.root / 'queue'

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_submission_during_claim_never_replaces_working_payload(self):
        handoff.submit(self.queue, ID, TURN)
        def consume(payload):
            working, = (self.queue / 'working').glob('*.json')
            before = working.read_bytes()
            self.assertEqual(handoff.submit(self.queue, ID, TURN)['status'], 'duplicate')
            self.assertEqual(working.read_bytes(), before)
            self.assertEqual(list((self.queue / 'pending').glob('*.json')), [])
            return {'status': 'renamed', 'title': '不进入共享队列的敏感标题'}
        self.assertEqual(handoff.run_worker(self.queue, consume)['processed'], 1)
        done, = (self.queue / 'done').glob('*.json')
        self.assertNotIn('敏感标题', done.read_text(encoding='utf-8'))
        self.assertEqual(handoff.submit(self.queue, ID, TURN)['status'], 'duplicate')

    def test_active_worker_excludes_other_process_and_prevents_reclaim(self):
        handoff.submit(self.queue, ID, TURN)
        def consume(payload):
            working, = (self.queue / 'working').glob('*.json')
            os.utime(working, (1, 1))
            code = ('import sys;sys.path.insert(0,sys.argv[1]);import handoff,json;'
                    'print(json.dumps(handoff.run_worker(sys.argv[2],lambda _: {"status":"kept"})))')
            proc = subprocess.run([sys.executable, '-c', code, str(ROOT / 'scripts'), str(self.queue)],
                                  capture_output=True, encoding='utf-8', timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)['status'], 'busy')
            self.assertTrue(working.exists())
            return {'status': 'kept'}
        handoff.run_worker(self.queue, consume)

    def test_orphaned_working_request_is_recovered_after_lock_release(self):
        request = handoff.submit(self.queue, ID, TURN)
        pending, working, _, _ = handoff._paths(self.queue, request['request_key'])
        working.parent.mkdir()
        os.replace(pending, working)
        process = Mock(return_value={'status': 'kept'})
        self.assertEqual(handoff.run_worker(self.queue, process)['processed'], 1)
        process.assert_called_once_with({'thread_id': ID, 'turn_id': TURN})
        self.assertFalse(working.exists())

    def test_retries_have_backoff_and_preserve_no_exception_text(self):
        handoff.submit(self.queue, ID, TURN)
        process = Mock(side_effect=RuntimeError('synthetic-secret'))
        for attempt in range(handoff.MAX_ATTEMPTS):
            with patch.object(handoff.time, 'time', return_value=1000 + attempt * 100):
                result = handoff.run_worker(self.queue, process)
            self.assertEqual(result['failed'], 1)
        self.assertEqual(process.call_count, handoff.MAX_ATTEMPTS)
        self.assertEqual(list((self.queue / 'pending').glob('*.json')), [])
        for file in self.queue.rglob('*.json'):
            self.assertNotIn('synthetic-secret', file.read_text(encoding='utf-8'))

    def test_unsettled_turn_is_deferred_without_model_failure(self):
        handoff.submit(self.queue, ID, TURN)
        result = handoff.run_worker(self.queue, lambda _: {'status': 'turn_not_settled'})
        self.assertEqual((result['retried'], result['failed']), (1, 0))
        process = Mock()
        handoff.run_worker(self.queue, process)
        process.assert_not_called()

    def test_invalid_payload_does_not_reach_consumer(self):
        request = handoff.submit(self.queue, ID, TURN)
        pending, _, _, _ = handoff._paths(self.queue, request['request_key'])
        value = handoff._read(pending)
        value['command'] = '不允许执行的队列字段'
        handoff._write(pending, value)
        process = Mock()
        self.assertEqual(handoff.run_worker(self.queue, process)['failed'], 1)
        process.assert_not_called()

    def test_detached_child_does_not_keep_parent_pipes_open(self):
        script = self.root / 'marker.py'
        marker = self.root / 'marker.txt'
        script.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("ok")\n', encoding='utf-8')
        code = 'import sys;sys.path.insert(0,sys.argv[1]);import handoff;print(handoff.spawn_worker(sys.argv[2]))'
        proc = subprocess.run([sys.executable, '-c', code, str(ROOT / 'scripts'), str(script)],
                              capture_output=True, encoding='utf-8', timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('True', proc.stdout)
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(marker.read_text(), 'ok')

    def hook(self, *, owner=True, task_enabled=False):
        app.atomic_json(self.root / 'config.json', {'worker_task_enabled': task_enabled})
        output = io.StringIO()
        event = {'hook_event_name': 'Stop', 'session_id': ID, 'turn_id': TURN, 'transcript_path': 'private-path'}
        with patch.object(app, 'data_dir', return_value=self.root), patch.object(app, 'owner_process', return_value=owner), \
             patch.object(app.handoff, 'queue_dir', return_value=self.queue), \
             patch.object(app.handoff, 'spawn_worker', return_value=True) as spawn, \
             patch.object(app.handoff, 'trigger_task', return_value=True) as task, \
             patch.object(sys, 'argv', ['title', 'hook']), patch.object(sys, 'stdin', io.StringIO(json.dumps(event))), \
             patch.object(sys, 'stdout', output), patch.dict(os.environ, {'OIL_CODEX_TITLE_WORKER': '0'}):
            code = app.main()
        return code, output.getvalue(), spawn, task

    def test_owner_hook_only_queues_and_starts_same_user_worker(self):
        code, output, spawn, task = self.hook()
        self.assertEqual((code, output), (0, '{}\n'))
        spawn.assert_called_once_with(ROOT / 'scripts/mogoo_codex_title.py')
        task.assert_not_called()
        request, = (self.queue / 'pending').glob('*.json')
        self.assertNotIn('private-path', request.read_text())

    def test_sandbox_hook_never_spawns_a_wrong_identity_worker(self):
        code, output, spawn, task = self.hook(owner=False, task_enabled=True)
        self.assertEqual((code, output), (0, '{}\n'))
        spawn.assert_not_called()
        task.assert_called_once()

    def test_unconfigured_sandbox_and_wrong_identity_consumer_fail_closed(self):
        _, output, spawn, task = self.hook(owner=False)
        self.assertEqual(output, '{}\n')
        spawn.assert_not_called()
        task.assert_not_called()
        self.assertEqual(list((self.queue / 'pending').glob('*.json')), [])
        with patch.object(app, 'owner_process', return_value=False):
            with self.assertRaises(app.BackendError):
                app.run_worker(self.root, app.DEFAULTS)

    def test_diagnostics_return_categories_without_raw_secrets(self):
        self.assertEqual(diagnostic_category('Access denied C:/private synthetic-secret'), 'permission_denied')
        self.assertEqual(diagnostic_category('HTTP 401 bearer synthetic-secret'), 'authentication_failed')
        self.assertEqual(diagnostic_category('Exceeded skills context budget'), 'skills_budget')

    def test_runner_template_is_valid_and_keeps_paths_out_of_source(self):
        text = RUNNER.format(home=repr('synthetic-home'), root=repr('synthetic-root'))
        compile(text, '<worker-runner>', 'exec')
        self.assertNotIn(str(Path.home()), RUNNER)


if __name__ == '__main__':
    unittest.main()
