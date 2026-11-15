"""从真实命名/归档入口验证省调用、失败记账与扫描预算，不调用外部模型。"""
import copy
import json
import io
import os
from pathlib import Path
import subprocess
import sys
import time
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import oil_codex_title as title
import archive_policy as archive
from codex_adapter import BackendError
from usage_ledger import usage_scope, usage_report, parse_usage
import test_title as fixtures
import test_archive as archive_fixtures

ID = fixtures.ID
USAGE = {'input_tokens': 37, 'cached_input_tokens': 20, 'output_tokens': 4, 'reasoning_output_tokens': 1}


class EfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backend = fixtures.FakeBackend()
        self.config = title.DEFAULTS.copy()
        self.candidate = {'action': 'rename', 'title': '🎬 产品视频｜讲解大纲', 'reason': '明确目标'}

    def tearDown(self):
        self.tmp.cleanup()

    def process(self, model=fixtures.proposal, **kw):
        return title.process_thread(self.backend, model, ID, self.root, self.config, apply=True, **kw)

    def append(self, text='好的', **extra):
        self.backend.thread['turns'].append({'id': str(len(self.backend.thread['turns'])),
            'status': 'completed', 'items': [{'type': 'userMessage', 'content': [{'type': 'text', 'text': text}, *extra.get('parts', [])]}]})

    def actual_model(self, context):
        return title.limited_title('unused', self.root, self.config, context,
            before_model=lambda: title.ensure_title_active(self.backend, ID, self.root))

    def fake_run(self, args, **kw):
        output = Path(args[args.index('--output-last-message') + 1])
        output.write_text(json.dumps(self.candidate), encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout=json.dumps({'type': 'turn.completed', 'usage': USAGE}))

    def records(self):
        return [json.loads(p.read_text(encoding='utf-8')) for p in (self.root / 'usage').glob('*/*.json')]

    def test_confirmations_skip_twice_then_model_rechecks(self):
        self.process()
        model = Mock(side_effect=fixtures.proposal)
        for text in ('好的！', 'continue.'):
            self.append(text)
            self.assertEqual(self.process(model)['skip_reason'], 'confirmation_only')
        model.assert_not_called()
        self.append('谢谢')
        self.assertNotIn('skip_reason', self.process(model))
        self.assertEqual(model.call_count, 1)
        self.assertEqual(title.read_json(title.state_path(self.root, ID))['confirmation_skips'], 0)

    def test_substantive_messages_and_attachments_still_call_model(self):
        for text, parts in [('好的，换成支付功能', []), ('推送', []), ('修复', []), ('okay?', []),
                            ('好的', [{'type': 'image', 'url': 'fixture'}]), ('改用英文命名', [])]:
            with self.subTest(text=text, parts=parts):
                self.backend = fixtures.FakeBackend()
                title.state_path(self.root, ID).unlink(missing_ok=True)
                self.process()
                self.append(text, parts=parts)
                model = Mock(side_effect=fixtures.proposal)
                self.assertNotIn('skip_reason', self.process(model))
                model.assert_called_once()

    def test_first_observation_and_policy_upgrade_require_model(self):
        self.backend.thread['name'] = self.candidate['title']
        self.backend.thread['turns'][0]['items'] = [{'type': 'userMessage', 'content': [{'type': 'text', 'text': '好的'}]}]
        model = Mock(side_effect=fixtures.proposal)
        self.process(model)
        model.assert_called_once()
        state = title.read_json(title.state_path(self.root, ID))
        state['policy_version'] = title.POLICY_VERSION - 1
        title.atomic_json(title.state_path(self.root, ID), state)
        self.append()
        self.process(model)
        self.assertEqual(model.call_count, 2)

    def test_confirmation_does_not_hide_unseen_substantive_turn(self):
        self.process()
        self.append('改为制作另一款产品的广告')
        self.append('好的')
        model = Mock(side_effect=fixtures.proposal)
        self.process(model)
        model.assert_called_once()

    def test_mutated_baseline_and_missing_baseline_cannot_skip(self):
        for change in ('content', 'id'):
            with self.subTest(change=change):
                self.backend = fixtures.FakeBackend()
                title.state_path(self.root, ID).unlink(missing_ok=True)
                self.process()
                self.append()
                if change == 'content':
                    self.backend.thread['turns'][0]['items'][0]['content'][0]['text'] = '新目标'
                else:
                    self.backend.thread['turns'][0]['id'] = 'missing'
                model = Mock(side_effect=fixtures.proposal)
                self.process(model)
                model.assert_called_once()

    def test_confirmation_still_respects_manual_title(self):
        self.process()
        self.append()
        self.backend.thread['name'] = '我的固定标题'
        model = Mock(side_effect=fixtures.proposal)
        self.assertEqual(self.process(model)['status'], 'manual_title')
        model.assert_not_called()

    def test_usage_survives_stale_result_and_keeps_no_content(self):
        def moved(args, **kwargs):
            result = self.fake_run(args, **kwargs)
            self.append('下一项工作')
            return result
        with patch('codex_adapter.subprocess.run', side_effect=moved):
            self.assertEqual(self.process(self.actual_model)['status'], 'stale_result')
        row, = self.records()
        self.assertEqual(row['usage'], USAGE)
        self.assertEqual((row['status'], row['outcome']), ('completed', 'stale_result'))
        self.assertNotIn('text', row)
        self.assertNotIn('title', row)
        self.assertNotIn('reason', row)

    def test_usage_survives_validation_error(self):
        self.candidate['title'] = '没有合法格式'
        with patch('codex_adapter.subprocess.run', side_effect=self.fake_run), self.assertRaises(ValueError):
            self.process(self.actual_model)
        row, = self.records()
        self.assertEqual(row['usage'], USAGE)
        self.assertEqual(row['outcome'], 'error')

    def test_json_failure_keeps_usage_before_parsing(self):
        def broken(args, **kwargs):
            result = self.fake_run(args, **kwargs)
            Path(args[args.index('--output-last-message') + 1]).write_text('{', encoding='utf-8')
            return result
        with patch('codex_adapter.subprocess.run', side_effect=broken), self.assertRaises(ValueError):
            self.process(self.actual_model)
        row, = self.records()
        self.assertEqual((row['status'], row['usage']), ('invalid_json', USAGE))

    def test_timeout_known_usage_and_unknown_launch_failure_are_distinct(self):
        output = json.dumps({'type': 'turn.completed', 'usage': USAGE}).encode()
        with patch('codex_adapter.subprocess.run', side_effect=subprocess.TimeoutExpired('codex', 1, output=output)):
            with self.assertRaises(BackendError): self.process(self.actual_model)
        with patch('codex_adapter.subprocess.run', side_effect=OSError('private details')):
            with self.assertRaises(OSError): self.process(self.actual_model)
        report = usage_report(self.root)
        group, = report['groups']
        self.assertEqual(group['attempts'], 2)
        self.assertEqual(group['unknown_usage'], 1)
        self.assertEqual(group['tokens'], {**USAGE, 'cache_write_input_tokens': 0})
        self.assertNotIn('private details', json.dumps(self.records()))

    def test_retry_has_two_records_without_aggregate_double_count(self):
        self.candidate = {'action': 'keep', 'title': '旧格式', 'reason': '保持'}
        with patch('codex_adapter.subprocess.run', side_effect=self.fake_run) as run:
            self.process(self.actual_model)
        self.assertEqual(run.call_count, 2)
        group, = usage_report(self.root)['groups']
        self.assertEqual(group['attempts'], 2)
        self.assertEqual(group['tokens']['input_tokens'], 74)

    def test_confirmation_and_archived_thread_create_no_model_records(self):
        with patch('codex_adapter.subprocess.run', side_effect=self.fake_run) as run:
            self.process(self.actual_model)
            self.append()
            self.process(self.actual_model)
            self.backend.archived = True
            self.process(self.actual_model)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(self.records()), 1)

    def test_scope_records_interruption_and_incomplete_usage(self):
        with patch('codex_adapter.subprocess.run', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.process(self.actual_model)
        row, = self.records()
        self.assertEqual((row['status'], row['usage']), ('interrupted', None))
        self.assertEqual(usage_report(self.root)['groups'][0]['unknown_usage'], 1)

    def test_archive_budget_prevents_reads_and_model(self):
        backend = archive_fixtures.Backend()
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex')}):
            archive.save_guards(self.root, {'pinnedThreads': [], 'threads': []})
            model = Mock()
            report = archive.scan(backend, self.root, model, budget_seconds=0)
        self.assertEqual(report['counts']['budget_exhausted'], 1)
        self.assertEqual(backend.reads, 0)
        model.assert_not_called()

    def test_archive_records_usage_even_when_content_changes_during_model(self):
        backend = archive_fixtures.Backend()
        self.candidate = {'classification': 'completed', 'reason': '已完成'}
        def moved(args, **kwargs):
            result = self.fake_run(args, **kwargs)
            backend.thread['turns'][0]['items'][0]['content'][0]['text'] = '新的未完成工作'
            return result
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex')}), patch('codex_adapter.subprocess.run', side_effect=moved):
            archive.save_guards(self.root, {'pinnedThreads': [], 'threads': []})
            result = archive.scan(backend, self.root,
                lambda context, **kw: archive.classify('unused', self.root, self.config, context, **kw))
        self.assertEqual(result['counts']['stale'], 1)
        row, = self.records()
        self.assertEqual((row['purpose'], row['outcome'], row['usage']), ('archiving', 'stale', USAGE))

    def test_archive_budget_limits_model_timeout(self):
        backend = archive_fixtures.Backend()
        self.candidate = {'classification': 'completed', 'reason': '已完成'}
        def timed(args, **kw):
            self.assertGreater(kw['timeout'], 0)
            self.assertLessEqual(kw['timeout'], 30)
            return self.fake_run(args, **kw)
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex')}), patch('codex_adapter.subprocess.run', side_effect=timed):
            archive.save_guards(self.root, {'pinnedThreads': [], 'threads': []})
            archive.scan(backend, self.root, lambda context, **kw: archive.classify('unused', self.root, self.config, context, **kw), budget_seconds=30)

    def test_usage_parser_tolerates_noise_and_rejects_invalid_fields(self):
        raw = '\n'.join(['noise', 'null', json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': -1, 'output_tokens': True}}), json.dumps({'type': 'turn.completed', 'usage': USAGE})])
        self.assertEqual(parse_usage(raw), (USAGE, False))

    def run_archive_cli(self, host, enabled=True, extra_args=()):
        title.atomic_json(self.root / 'archive/config.json', {'enabled': enabled})
        output = io.StringIO()
        backend = archive_fixtures.Backend()
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex')}), \
             patch.object(archive, 'data_dir', return_value=self.root), \
             patch.object(archive, 'find_codex', return_value='unused') as binary, \
             patch.object(archive, 'CodexBackend') as factory, \
             patch.object(sys, 'argv', ['archive', 'scan', '--scheduled', '--guards-stdin', '--current-id', ID, '--limit', '0', *extra_args]), \
             patch.object(sys, 'stdin', io.StringIO(json.dumps(host))), patch.object(sys, 'stdout', output):
            factory.return_value.__enter__.return_value = backend
            code = archive.main()
        return code, json.loads(output.getvalue()), binary

    def test_combined_guard_scan_returns_real_fresh_report(self):
        title.atomic_json(self.root / 'archive/preview.json', {'created_at': 1, 'counts': {'listed': 999}})
        code, report, _ = self.run_archive_cli({'pinnedThreads': [], 'threads': []})
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'preview')
        self.assertGreater(report['created_at'], 1)
        self.assertEqual(report['counts'], {'listed': 1, 'protected': 1})
        self.assertEqual(report, title.read_json(self.root / 'archive/preview.json'))
        self.assertIn(ID, title.read_json(self.root / 'archive/guards.json')['protected_ids'])

    def test_combined_scan_rejects_bad_or_incomplete_host_before_backend(self):
        for host in ([], {}, {'pinnedThreads': [], 'threads': [], 'unavailableHosts': ['local']}):
            with self.subTest(host=host):
                code, report, binary = self.run_archive_cli(host)
                self.assertEqual(code, 1)
                self.assertEqual(report['status'], 'error')
                binary.assert_not_called()

    def test_disabled_combined_scan_needs_no_host_or_backend(self):
        code, report, binary = self.run_archive_cli(None, enabled=False)
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'disabled')
        binary.assert_not_called()
        self.assertFalse((self.root / 'archive/guards.json').exists())

    def test_archive_cli_budget_seconds_flag(self):
        host = {'pinnedThreads': [], 'threads': []}
        for extra, expected in (((), archive.DEFAULT_BUDGET_SECONDS), (('--budget-seconds', '300'), 300)):
            with self.subTest(extra=extra), patch.object(archive, 'scan', return_value={'status': 'preview', 'counts': {}, 'candidates': []}) as mocked:
                code, report, _ = self.run_archive_cli(host, extra_args=extra)
            self.assertEqual(code, 0)
            self.assertEqual(report['status'], 'preview')
            self.assertEqual(mocked.call_args.kwargs['budget_seconds'], expected)
        for bad in ('0', '601'):
            with self.subTest(bad=bad):
                code, report, binary = self.run_archive_cli(host, extra_args=('--budget-seconds', bad))
                self.assertEqual(code, 1)
                self.assertEqual(report['status'], 'error')
                binary.assert_not_called()

    def test_archive_min_window_skips_without_writing_uncertain_and_recovers(self):
        self.candidate = {'classification': 'completed', 'reason': '已完成'}
        clock = [1000.0]
        backend = archive_fixtures.Backend()
        title.atomic_json(self.root / 'archive/config.json', {'enabled': True})
        archive.save_guards(self.root, {'pinnedThreads': [], 'threads': []})
        cfg = {**self.config, 'model_timeout_seconds': 45}
        model = lambda context, **kw: archive.classify('unused', self.root, cfg, context, **kw)
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex')}), \
             patch('time.monotonic', side_effect=lambda: clock[0]), \
             patch('codex_adapter.subprocess.run', side_effect=self.fake_run) as run:
            first = archive.scan(backend, self.root, model, budget_seconds=15)
        self.assertEqual(first['counts']['budget_exhausted'], 1)
        run.assert_not_called()
        self.assertFalse(archive.record_path(self.root, ID).exists())

        @contextmanager
        def delayed_slot(*args):
            clock[0] += 12.0
            yield True

        with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex')}), \
             patch('time.monotonic', side_effect=lambda: clock[0]), \
             patch.object(archive, 'worker_slot', delayed_slot), \
             patch('codex_adapter.subprocess.run', side_effect=self.fake_run) as run:
            second = archive.scan(backend, self.root, model, budget_seconds=30)
        self.assertEqual(second['counts']['budget_exhausted'], 1)
        run.assert_not_called()
        self.assertFalse(archive.record_path(self.root, ID).exists())

        with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex')}), \
             patch('time.monotonic', side_effect=lambda: clock[0]), \
             patch('codex_adapter.subprocess.run', side_effect=self.fake_run) as run:
            third = archive.scan(backend, self.root, model, budget_seconds=150)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(third['candidates']), 1)

    def test_archive_model_budget_exhausted_does_not_write_uncertain(self):
        self.candidate = {'classification': 'completed', 'reason': '已完成'}
        cfg = {**self.config, 'model_timeout_seconds': 45}
        model = lambda context, **kw: archive.classify('unused', self.root, cfg, context, **kw)
        for wait_seconds, count_key in ((30.0, 'model_budget_exhausted'), (45.0, 'errors')):
            with self.subTest(wait_seconds=wait_seconds):
                clock = [1000.0]
                backend = archive_fixtures.Backend()
                title.atomic_json(self.root / 'archive/config.json', {'enabled': True})
                archive.save_guards(self.root, {'pinnedThreads': [], 'threads': []})
                archive.record_path(self.root, ID).unlink(missing_ok=True)

                @contextmanager
                def slow_slot(*args):
                    clock[0] += wait_seconds
                    yield True

                with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex')}), \
                     patch('time.monotonic', side_effect=lambda: clock[0]), \
                     patch.object(archive, 'worker_slot', slow_slot), \
                     patch('codex_adapter.subprocess.run', side_effect=self.fake_run) as run:
                    skipped = archive.scan(backend, self.root, model, budget_seconds=300)
                self.assertEqual(skipped['counts'][count_key], 1)
                run.assert_not_called()
                self.assertFalse(archive.record_path(self.root, ID).exists())

                with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex')}), \
                     patch('codex_adapter.subprocess.run', side_effect=self.fake_run) as run:
                    recovered = archive.scan(backend, self.root, model, budget_seconds=300)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(len(recovered['candidates']), 1)

    def test_archive_single_eval_host_calls_deduplicated(self):
        self.candidate = {'classification': 'completed', 'reason': '已完成'}
        is_archived_calls = 0

        class CountingBackend(archive_fixtures.Backend):
            def is_archived(self, tid, cwd=None):
                nonlocal is_archived_calls
                is_archived_calls += 1
                return super().is_archived(tid, cwd)

        backend = CountingBackend()
        title.atomic_json(self.root / 'archive/config.json', {'enabled': True})
        archive.save_guards(self.root, {'pinnedThreads': [], 'threads': []})
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex')}), \
             patch('codex_adapter.subprocess.run', side_effect=self.fake_run):
            result = archive.scan(backend, self.root,
                lambda context, **kw: archive.classify('unused', self.root, self.config, context, **kw))
        self.assertEqual(backend.reads, 3)
        self.assertEqual(is_archived_calls, 2)
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(len(self.records()), 1)

    def test_archive_synthetic_benchmark_70_recent_and_10_evaluable(self):
        self.candidate = {'classification': 'completed', 'reason': '已完成'}

        class SyntheticBackend:
            def __init__(self, clock, threads):
                self.clock = clock
                self.threads = {t['id']: t for t in threads}
                self.order = [t['id'] for t in threads]
                self.reads = 0
                self.is_archived_calls = 0

            def list_threads(self, *, archived=False, cwd=None):
                if archived:
                    return iter([])
                return iter(copy.deepcopy(self.threads[tid]) for tid in self.order)

            def read(self, tid):
                self.reads += 1
                self.clock[0] += 0.2
                return copy.deepcopy(self.threads[tid])

            def is_archived(self, tid, cwd=None):
                self.is_archived_calls += 1
                self.clock[0] += 0.2
                return False

        now = archive.time.time()
        def make_threads():
            items = []
            for idx in range(70):
                tid = f'00000000-0000-0000-0000-{idx + 1:012d}'
                items.append({'id': tid, 'name': f'📝 最近｜任务{idx}', 'source': 'vscode', 'cwd': '/sample',
                              'status': {'type': 'notLoaded'}, 'updatedAt': now - 3600,
                              'turns': [{'id': 't1', 'status': 'completed', 'completedAt': now - archive.DAY,
                                         'items': [{'type': 'userMessage', 'content': [{'type': 'text', 'text': 'recent work'}]},
                                                   {'type': 'agentMessage', 'phase': 'final_answer', 'text': 'done'}]}]})
            for idx in range(10):
                tid = f'11111111-1111-1111-1111-{idx + 1:012d}'
                items.append({'id': tid, 'name': f'🎨 旧任务｜评估{idx}', 'source': 'vscode', 'cwd': '/sample',
                              'status': {'type': 'notLoaded'}, 'updatedAt': now - 20 * archive.DAY,
                              'turns': [{'id': 't1', 'status': 'completed', 'completedAt': now - 20 * archive.DAY,
                                         'items': [{'type': 'userMessage', 'content': [{'type': 'text', 'text': f'design item {idx}'}]},
                                                   {'type': 'agentMessage', 'phase': 'final_answer', 'text': 'delivered'}]}]})
            return items

        def run_case(root, budget):
            clock = [1000.0]
            backend = SyntheticBackend(clock, make_threads())
            title.atomic_json(root / 'archive/config.json', {'enabled': True})
            archive.save_guards(root, {'pinnedThreads': [], 'threads': []})
            cfg = {**self.config, 'model_timeout_seconds': 45}
            def slow_model(args, **kw):
                clock[0] += 13.0
                return self.fake_run(args, **kw)
            with patch.dict(os.environ, {'CODEX_HOME': str(root / 'codex')}), \
                 patch('time.monotonic', side_effect=lambda: clock[0]), \
                 patch('codex_adapter.subprocess.run', side_effect=slow_model) as run:
                report = archive.scan(backend, root,
                    lambda context, **kw: archive.classify('unused', root, cfg, context, **kw),
                    budget_seconds=budget)
            return clock[0] - 1000.0, backend, report, run.call_count

        elapsed_150, b_150, r_150, calls_150 = run_case(self.root, 150)
        self.assertAlmostEqual(elapsed_150, 140.2, places=6)
        self.assertEqual(calls_150, 9)
        self.assertEqual(r_150['counts']['model_attempts'], 9)
        self.assertEqual(r_150['counts']['budget_exhausted'], 1)
        self.assertEqual(r_150['counts'].get('errors', 0), 0)
        self.assertEqual(len(r_150['candidates']), 9)
        last_tid = '11111111-1111-1111-1111-000000000010'
        self.assertFalse(archive.record_path(self.root, last_tid).exists())

        with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex')}), \
             patch('codex_adapter.subprocess.run', side_effect=self.fake_run) as run:
            followup = archive.scan(b_150, self.root,
                lambda context, **kw: archive.classify('unused', self.root, self.config, context, **kw),
                budget_seconds=150)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(followup['candidates']), 10)

        with tempfile.TemporaryDirectory() as other:
            root_300 = Path(other)
            elapsed_300, b_300, r_300, calls_300 = run_case(root_300, 300)
        self.assertAlmostEqual(elapsed_300, 154.0, places=6)
        self.assertEqual(b_300.reads + b_300.is_archived_calls, 120)
        self.assertEqual(calls_300, 10)
        self.assertEqual(r_300['counts']['model_attempts'], 10)
        self.assertEqual(len(r_300['candidates']), 10)


if __name__ == '__main__':
    unittest.main()
