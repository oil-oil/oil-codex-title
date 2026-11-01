"""有界移交队列；认领、重复投递和恢复共用内核锁，不存放对话内容。"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

import file_lock

MAX_ATTEMPTS = 3
MAX_REQUEST_BYTES = 4096


class DeferredRequest(Exception):
    pass


def queue_dir(root):
    root = Path(root).resolve()
    if os.name != 'nt':
        return root / 'handoff'
    identity = hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()[:16]
    return Path(os.environ.get('PROGRAMDATA', tempfile.gettempdir())) / 'MogooOilCodexTitle' / identity / 'queue'


def task_name(root):
    identity = hashlib.sha256(os.path.normcase(str(Path(root).resolve())).encode()).hexdigest()[:16]
    return 'MogooOilCodexTitleWorker-' + identity


def _ids(thread_id, turn_id):
    return str(uuid.UUID(thread_id)), str(uuid.UUID(turn_id))


def _key(thread_id, turn_id):
    return hashlib.sha256((thread_id + ':' + turn_id).encode()).hexdigest()


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.queue-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _read(path):
    if path.stat().st_size > MAX_REQUEST_BYTES:
        raise ValueError('移交请求过大')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('移交请求格式错误')
    return value


@contextmanager
def _lock(path, *, wait=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        deadline = time.monotonic() + wait
        while True:
            try:
                file_lock.acquire(stream)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.02)
        try:
            yield True
        finally:
            file_lock.release(stream)


def _paths(queue, key):
    queue = Path(queue)
    return (queue / 'pending' / (key + '.json'), queue / 'working' / (key + '.json'),
            queue / 'done' / (key + '.json'), queue / 'locks' / (key + '.lock'))


def submit(queue, thread_id, turn_id):
    thread_id, turn_id = _ids(thread_id, turn_id)
    key = _key(thread_id, turn_id)
    pending, working, done, lock = _paths(queue, key)
    with _lock(lock, wait=1) as acquired:
        if not acquired:
            raise OSError('移交请求正在操作，请稍后重试')
        for path in (done, working, pending):
            if path.exists():
                return {'status': 'duplicate', 'request_key': key}
        _write(pending, {'version': 1, 'thread_id': thread_id, 'turn_id': turn_id,
                         'attempts': 0, 'submitted_at': time.time(), 'retry_after': 0})
    return {'status': 'queued', 'request_key': key}


def _validate(path, value):
    allowed = {'version', 'thread_id', 'turn_id', 'attempts', 'submitted_at', 'retry_after'}
    if set(value) - allowed or type(value.get('version')) is not int or value.get('version') != 1:
        raise ValueError('移交请求字段无效')
    thread_id, turn_id = _ids(value.get('thread_id'), value.get('turn_id'))
    if _key(thread_id, turn_id) != path.stem:
        raise ValueError('移交请求与文件标识不一致')
    if type(value.get('attempts')) is not int or not 0 <= value['attempts'] < MAX_ATTEMPTS:
        raise ValueError('移交重试次数无效')
    for key in ('submitted_at', 'retry_after'):
        if type(value.get(key)) not in (int, float) or not 0 <= value[key] < 1e12:
            raise ValueError('移交时间字段无效')
    return value


def _finish(queue, key, value, status):
    _, working, done, _ = _paths(queue, key)
    # 共享目录中只存状态，不写回标题、用量明细或错误原文。
    _write(done, {'version': 1, 'thread_id': value.get('thread_id'), 'turn_id': value.get('turn_id'),
                  'status': status, 'finished_at': time.time(), 'attempts': value.get('attempts', 0)})
    working.unlink(missing_ok=True)


def _recover(queue):
    # 仅在持有整个 worker 的内核锁时调用；活跃 worker 不会被超时误回收。
    folder = Path(queue) / 'working'
    for working in folder.glob('*.json'):
        pending, _, done, lock = _paths(queue, working.stem)
        with _lock(lock, wait=1) as acquired:
            if not acquired:
                continue
            if done.exists():
                working.unlink(missing_ok=True)
            elif not pending.exists():
                os.replace(working, pending)


def run_worker(queue, process_one, *, limit=4):
    queue = Path(queue)
    stats = {'processed': 0, 'failed': 0, 'retried': 0, 'status': 'completed'}
    with _lock(queue / 'worker.lock') as acquired:
        if not acquired:
            return {**stats, 'status': 'busy'}
        _recover(queue)
        folder = queue / 'pending'
        paths = sorted(folder.glob('*.json'), key=lambda p: p.stat().st_mtime)
        for pending in paths[:limit]:
            _, working, done, lock = _paths(queue, pending.stem)
            with _lock(lock, wait=1) as claimed:
                if not claimed or not pending.exists() or working.exists() or done.exists():
                    continue
                try:
                    value = _validate(pending, _read(pending))
                except (OSError, ValueError, TypeError, AttributeError):
                    working.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(pending, working)
                    _finish(queue, pending.stem, {}, 'invalid_request')
                    stats['failed'] += 1
                    continue
                if value['retry_after'] > time.time():
                    continue
                working.parent.mkdir(parents=True, exist_ok=True)
                os.replace(pending, working)
            try:
                result = process_one({'thread_id': value['thread_id'], 'turn_id': value['turn_id']})
                status = result.get('status', 'completed')
                if status in ('turn_not_settled', 'busy'):
                    raise DeferredRequest()
            except Exception as exc:
                if not isinstance(exc, DeferredRequest):
                    stats['failed'] += 1
                with _lock(lock, wait=1) as acquired:
                    if not acquired:
                        raise OSError('无法记录移交失败')
                    value['attempts'] += 1
                    if value['attempts'] >= MAX_ATTEMPTS:
                        _finish(queue, pending.stem, value, 'failed')
                    else:
                        value['retry_after'] = time.time() + 30 * value['attempts']
                        _write(pending, value)
                        working.unlink(missing_ok=True)
                        stats['retried'] += 1
                continue
            with _lock(lock, wait=1) as acquired:
                if not acquired:
                    raise OSError('无法记录移交完成')
                _finish(queue, pending.stem, value, status)
            stats['processed'] += 1
    return stats


def spawn_worker(script):
    env = os.environ.copy()
    for key in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CODEX_APP_TOOLS_PIPE_PATH'):
        env.pop(key, None)
    options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL,
               'close_fds': True, 'env': env}
    if os.name == 'nt':
        options['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options['start_new_session'] = True
    try:
        subprocess.Popen([sys.executable, '-X', 'utf8', str(script), 'worker'], **options)
        return True
    except OSError:
        return False


def trigger_task(name):
    # 任务可能不允许沙箱身份主动触发；周期任务仍负责消费，不把拒绝当作换身份成功。
    try:
        subprocess.Popen(['schtasks.exe', '/Run', '/TN', name], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                         creationflags=subprocess.CREATE_NO_WINDOW)
        return True
    except OSError:
        return False
