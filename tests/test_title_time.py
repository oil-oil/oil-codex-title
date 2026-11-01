"""最后用户消息时间、标题保护和无模型时间更新回归，全部使用合成会话。"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_title as fixtures
import oil_codex_title as app
import title_time

TZ = timezone(timedelta(hours=8))
FIRST = datetime(2026, 10, 8, 14, 30, tzinfo=TZ).timestamp()
SECOND = FIRST + 5 * 60
BASE = '🎬 产品视频｜讲解大纲'


class TitleTimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backend = fixtures.FakeBackend()
        self.config = app.DEFAULTS.copy()
        self.backend.thread['turns'][0]['items'][0]['createdAt'] = FIRST
        render = app.display_title
        self.local_time = patch.object(app, 'display_title', side_effect=lambda *args: render(*args, tz=TZ))
        self.local_time.start()

    def tearDown(self):
        self.local_time.stop()
        self.tmp.cleanup()

    def process(self, model=fixtures.proposal, **kwargs):
        return app.process_thread(self.backend, model, fixtures.ID, self.root, self.config, **kwargs)

    def test_appends_exact_user_time_and_keeps_model_input_time_free(self):
        model = Mock(side_effect=fixtures.proposal)
        result = self.process(model, apply=True)
        self.assertEqual(result['title'], BASE + ' · 10-08 14:30')
        self.backend.thread['turns'].append({'id': fixtures.NEW_TURN, 'status': 'completed', 'items': [
            {'type': 'userMessage', 'createdAt': SECOND, 'content': [{'type': 'text', 'text': '补充一个实质需求'}]}]})
        self.process(model, apply=True)
        self.assertEqual(model.call_args.args[0]['current_title'], BASE)
        self.assertEqual(self.backend.thread['name'], BASE + ' · 10-08 14:35')

    def test_timestamp_only_update_needs_no_model_and_has_stable_baseline(self):
        self.process(apply=True)
        self.backend.thread['turns'][0]['items'][0]['createdAt'] = SECOND
        model = Mock(side_effect=AssertionError('时间变化不能调用模型'))
        result = self.process(model, apply=True)
        self.assertEqual((result['status'], result['time_updated']), ('renamed', True))
        self.assertEqual(result['title'], BASE + ' · 10-08 14:35')
        self.assertEqual(self.process(model, apply=True)['status'], 'unchanged')
        model.assert_not_called()
        self.assertFalse(app.read_json(app.state_path(self.root, fixtures.ID)).get('locked'))

    def test_confirmation_updates_timestamp_without_model(self):
        self.process(apply=True)
        self.backend.thread['turns'].append({'id': fixtures.NEW_TURN, 'status': 'completed', 'items': [
            {'type': 'userMessage', 'createdAt': SECOND, 'content': [{'type': 'text', 'text': '好的'}]}]})
        model = Mock()
        result = self.process(model, apply=True)
        self.assertEqual(result['skip_reason'], 'confirmation_only')
        self.assertEqual(result['title'], BASE + ' · 10-08 14:35')
        model.assert_not_called()

    def test_preview_does_not_write_and_manual_title_is_protected(self):
        self.assertEqual(self.process()['title'], BASE + ' · 10-08 14:30')
        self.assertFalse(app.state_path(self.root, fixtures.ID).exists())
        self.process(apply=True)
        self.backend.thread['name'] = '我的固定标题 · 10-08 15:00'
        model = Mock()
        self.assertEqual(self.process(model, apply=True)['status'], 'manual_title')
        model.assert_not_called()
        self.assertEqual(self.backend.thread['name'], '我的固定标题 · 10-08 15:00')

    def test_pending_write_recovers_base_without_false_manual_lock(self):
        self.backend.thread['name'] = BASE + ' · 10-08 14:30'
        app.atomic_json(app.state_path(self.root, fixtures.ID), {'pending_title': self.backend.thread['name'],
            'pending_base_title': BASE, 'last_base_title': BASE, 'last_seen_title': '讨论事情'})
        self.assertEqual(self.process(apply=True)['status'], 'kept')
        state = app.read_json(app.state_path(self.root, fixtures.ID))
        self.assertEqual(state['last_base_title'], BASE)
        self.assertNotIn('pending_title', state)
        self.assertNotIn('pending_base_title', state)
        self.assertFalse(state.get('locked'))

    def test_disable_removes_managed_suffix_without_model(self):
        self.process(apply=True)
        self.config['show_last_user_time'] = False
        app.atomic_json(self.root / 'config.json', {'show_last_user_time': False})
        model = Mock()
        self.assertEqual(self.process(model, apply=True)['title'], BASE)
        model.assert_not_called()

    def test_user_written_date_remains_part_of_core_when_switch_is_disabled(self):
        written = '📅 节日活动｜安排 · 12-30 21:00'
        self.backend.thread['name'] = written
        keep = lambda _: ({'action': 'keep', 'title': written, 'reason': '保持任务日期'}, {})
        self.assertEqual(self.process(keep, apply=True)['title'], written + ' · 10-08 14:30')
        self.config['show_last_user_time'] = False
        app.atomic_json(self.root / 'config.json', {'show_last_user_time': False})
        model = Mock()
        self.assertEqual(self.process(model, apply=True)['title'], written)
        model.assert_not_called()

    def test_failed_base_write_does_not_replace_last_successful_base(self):
        self.process(apply=True)
        self.backend.thread['turns'][0]['items'][0]['content'][0]['text'] = '改为新版视频大纲'
        candidate = {'action': 'rename', 'title': '🎬 产品视频｜新版大纲', 'reason': '目标改变'}
        with patch.object(self.backend, 'rename', side_effect=app.BackendError('模拟写入失败')):
            with self.assertRaises(app.BackendError):
                self.process(lambda _: (candidate, {}), apply=True)
        state = app.read_json(app.state_path(self.root, fixtures.ID))
        self.assertEqual(state['last_base_title'], BASE)
        self.assertEqual(state['pending_base_title'], candidate['title'])
        model = Mock(return_value=(candidate, {}))
        self.process(model, apply=True)
        self.assertEqual(model.call_args.args[0]['current_title'], BASE)

    def test_time_only_updates_respect_archive_pause_lock_and_stale_content(self):
        for guard in ('archived', 'disabled', 'locked', 'stale'):
            with self.subTest(guard=guard):
                self.backend = fixtures.FakeBackend()
                self.backend.thread['turns'][0]['items'][0]['createdAt'] = FIRST
                app.state_path(self.root, fixtures.ID).unlink(missing_ok=True)
                (self.root / 'config.json').unlink(missing_ok=True)
                self.process(apply=True)
                self.backend.thread['turns'][0]['items'][0]['createdAt'] = SECOND
                writes = len(self.backend.writes)
                if guard == 'archived':
                    self.backend.archived = True
                elif guard == 'disabled':
                    app.atomic_json(self.root / 'config.json', {'enabled': False})
                elif guard == 'locked':
                    state = app.read_json(app.state_path(self.root, fixtures.ID))
                    state['locked'] = True
                    app.atomic_json(app.state_path(self.root, fixtures.ID), state)
                if guard == 'stale':
                    original = self.backend.read
                    count = 0
                    def read(thread_id):
                        nonlocal count
                        count += 1
                        if count == 2:
                            self.backend.thread['turns'][0]['items'][0]['content'][0]['text'] = '下一项实质工作'
                        return original(thread_id)
                    with patch.object(self.backend, 'read', side_effect=read):
                        self.assertEqual(self.process(Mock(), apply=True)['status'], 'stale_result')
                else:
                    self.assertEqual(self.process(Mock(), apply=True)['status'], guard)
                self.assertEqual(len(self.backend.writes), writes)

    def test_base_title_conflicts_cannot_be_hidden_by_different_times(self):
        self.backend.thread['cwd'] = 'synthetic-project'
        scope = app.snapshot(self.backend.thread, self.config)['scope_key']
        app.atomic_json(app.state_path(self.root, fixtures.NEW_TURN), {'scope_key': scope,
            'last_seen_title': BASE + ' · 10-07 10:00', 'last_base_title': BASE})
        self.assertEqual(self.process(apply=True)['status'], 'ambiguous_title')
        self.assertEqual(self.backend.writes, [])

    def test_latest_user_time_ignores_assistant_and_thread_updates(self):
        thread = self.backend.thread
        thread['updatedAt'] = SECOND + 3000
        thread['turns'][0]['completedAt'] = SECOND + 2000
        thread['turns'][0]['items'][1]['createdAt'] = SECOND + 1000
        self.assertEqual(title_time.last_user_timestamp(thread), FIRST)

    def test_rollout_matches_latest_user_and_turn_with_create_time_preferred(self):
        message = self.backend.thread['turns'][0]['items'][0]
        message.pop('createdAt')
        raw = message['content'][0]['text']
        path = self.root / 'synthetic-rollout.jsonl'
        rows = [
            {'type': 'response_item', 'timestamp': '2026-10-08T06:40:00Z', 'payload': {'type': 'message',
             'role': 'user', 'content': [{'type': 'input_text', 'text': raw}],
             'internal_chat_message_metadata_passthrough': {'turn_id': fixtures.TURN, 'create_time': FIRST}}},
            {'type': 'response_item', 'timestamp': '2026-10-08T08:00:00Z', 'payload': {'type': 'message',
             'role': 'user', 'content': [{'type': 'input_text', 'text': raw}],
             'internal_chat_message_metadata_passthrough': {'turn_id': fixtures.NEW_TURN, 'create_time': SECOND}}},
        ]
        path.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in rows), encoding='utf-8')
        self.backend.thread['path'] = str(path)
        self.assertEqual(title_time.last_user_timestamp(self.backend.thread), FIRST)
        self.assertEqual(self.process(apply=True)['title'], BASE + ' · 10-08 14:30')

    def test_cli_rollout_without_metadata_requires_matching_turn_boundary(self):
        message = self.backend.thread['turns'][0]['items'][0]
        message.pop('createdAt')
        path = self.root / 'cli-rollout.jsonl'
        row = {'type': 'response_item', 'timestamp': '2026-10-08T06:30:00Z', 'payload': {'type': 'message',
            'role': 'user', 'content': [{'type': 'input_text', 'text': message['content'][0]['text']}]}}
        for turn, expected in ((fixtures.TURN, FIRST), (fixtures.NEW_TURN, None)):
            boundary = {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': turn}}
            path.write_text(json.dumps(boundary) + '\n' + json.dumps(row) + '\n', encoding='utf-8')
            self.backend.thread['path'] = str(path)
            self.assertEqual(title_time.last_user_timestamp(self.backend.thread), expected)

    def test_missing_invalid_and_out_of_scan_timestamps_never_use_now(self):
        user = self.backend.thread['turns'][0]['items'][0]
        for value in (None, True, -1, 10 ** 1000, float('nan'), 'invalid', '2026-10-08T14:30:00'):
            user['createdAt'] = value
            self.assertEqual(title_time.display_title(BASE, self.backend.thread, tz=TZ), BASE)
        user.pop('createdAt')
        path = self.root / 'bounded.jsonl'
        row = {'type': 'response_item', 'timestamp': '2026-10-08T06:30:00Z', 'payload': {'type': 'message',
            'role': 'user', 'content': [{'type': 'input_text', 'text': user['content'][0]['text']}],
            'internal_chat_message_metadata_passthrough': {'turn_id': fixtures.TURN}}}
        path.write_text(json.dumps(row) + '\n' + 'noise\n' * 100, encoding='utf-8')
        self.backend.thread['path'] = str(path)
        with patch.object(title_time, 'MAX_SCAN_BYTES', 100):
            self.assertIsNone(title_time.last_user_timestamp(self.backend.thread))

    def test_timezone_milliseconds_and_long_core_preserve_title(self):
        user = self.backend.thread['turns'][0]['items'][0]
        user['createdAt'] = FIRST * 1000
        core = '🧩 ' + 'a' * 20 + '｜' + 'b' * 20
        self.assertEqual(title_time.display_title(core, self.backend.thread, tz=TZ), core + ' · 10-08 14:30')
        self.assertEqual(title_time.display_title(BASE, self.backend.thread, tz=timezone.utc), BASE + ' · 10-08 06:30')


if __name__ == '__main__':
    unittest.main()
