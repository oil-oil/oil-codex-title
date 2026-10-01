"""Windows 沙箱身份下的命名移交：Hook 只投递，用户身份 worker 执行。

背景（本机实测）：Codex 在 Windows 上把 Stop Hook 放进沙箱账户执行
（身份形如 ``<机器>\\CodexSandboxOffline``）。该身份对 ``~/.codex`` 只有
``ReadAndExecute``，因此 Hook 进程既写不了插件状态目录，也起不了 app-server
（sqlite 初始化 ``Access denied``），更连不上真实用户 daemon 的 control socket
（``WSAEACCES``，即"以一种访问权限不允许的方式做了一个访问套接字的尝试"）。

约定：Hook 侧只做**能力探测**。发现自己写不了状态目录时，把命名请求原子投递到
共享队列，然后照常向宿主输出空 JSON 并退出 0；由**用户身份**的 worker
（``oil_codex_title.py worker``，可由计划任务或 Codex automation 周期触发）
取走请求，复用与 Hook 完全相同的命名流程写回标题。

边界：

- 请求只携带话题与轮次标识，**不含**对话内容或转写文件路径；
- 本模块不触达 app-server，也不改写任何对话数据库或桌面私有状态；
- 队列是纯文件协作，读写都走 ``mkstemp`` + ``os.replace``，不留半截文件。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

REQUESTS = "requests"
DONE = "done"
FAILED = "failed"
LOG_NAME = "handoff.log"
MAX_ATTEMPTS = 3


def queue_dir(config=None) -> Path:
    """队列根目录：配置项 handoff_dir > 环境变量 > ``%PROGRAMDATA%`` 下的固定位置。

    两侧身份（沙箱账户与真实用户）看到的 ``%PROGRAMDATA%`` 相同，所以默认位置
    不依赖任何一方的用户目录。
    """
    override = config.get("handoff_dir") if isinstance(config, dict) else None
    override = override or os.environ.get("OIL_CODEX_TITLE_QUEUE")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("PROGRAMDATA") or tempfile.gettempdir()
    return Path(base) / "oil-codex-title" / "queue"


def _probe_write(directory: Path) -> None:
    """真实写入并按字节数校验一个探针文件；不可写时抛 OSError。"""
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".probe-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("ok")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if os.path.exists(name):
            os.unlink(name)


def writable(directory) -> bool:
    try:
        _probe_write(Path(directory))
        return True
    except OSError:
        return False


def state_writable(root) -> bool:
    """插件状态目录是否可写；不可写即表示当前进程处在沙箱身份下。"""
    return writable(Path(root))


def request_path(queue, thread_id: str, turn_id: str | None) -> Path:
    return Path(queue) / REQUESTS / f"{thread_id}__{turn_id or 'latest'}.json"


def submit(queue, payload: dict) -> Path:
    """原子投递；同一话题同一轮次重复触发时覆盖旧请求，不产生重复命名。"""
    queue = Path(queue)
    for folder in (REQUESTS, DONE, FAILED):
        (queue / folder).mkdir(parents=True, exist_ok=True)
    target = request_path(queue, payload["thread_id"], payload.get("turn_id"))
    fd, name = tempfile.mkstemp(prefix=".submit-", dir=queue / REQUESTS)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return target


def read_request(path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def pending(queue) -> list[Path]:
    folder = Path(queue) / REQUESTS
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.glob("*.json") if path.is_file())


def _archive(path: Path, folder: Path, payload: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    name = path.name[:-len(".working")] if path.name.endswith(".working") else path.name
    target = folder / name
    _write_json(target, payload)
    path.unlink(missing_ok=True)
    return target


def _write_json(path: Path, payload: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def record_failure(path, error: str, *, max_attempts: int = MAX_ATTEMPTS) -> Path:
    """记一次失败；达到上限后移入 failed/，不再重试。"""
    path = Path(path)
    payload = read_request(path)
    attempts = int(payload.get("attempts", 0)) + 1
    payload.update(attempts=attempts, last_error=str(error)[:500], last_failed_at=time.time())
    if attempts >= max_attempts:
        return _archive(path, path.parent.parent / FAILED, payload)
    # 放回待处理：下次 worker 立即重试，而不是等 .working 超时。
    restored = path.with_name(path.name[:-len(".working")])
    _write_json(restored, payload)
    if restored != path:
        path.unlink(missing_ok=True)
    return restored


def log(queue, message: str) -> None:
    """尽力写一行移交日志；沙箱身份下队列目录往往是唯一可写位置。"""
    try:
        queue = Path(queue)
        queue.mkdir(parents=True, exist_ok=True)
        with (queue / LOG_NAME).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "by": os.environ.get("USERNAME") or "",
                "message": message,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def hand_off(config, thread_id: str, turn_id: str | None) -> Path:
    """Hook 侧调用：投递一次命名请求。只写标识，不写对话内容。"""
    queue = queue_dir(config)
    payload = {
        "version": 1,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "hook_event_name": "Stop",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "submitted_by": os.environ.get("USERNAME") or "",
    }
    path = submit(queue, payload)
    log(queue, f"移交 {thread_id} turn={turn_id or '-'}")
    return path


def _claim(path: Path):
    """把待处理请求改名为 .working，避免两个 worker 抢同一条。"""
    target = path.with_name(path.name + ".working")
    try:
        os.replace(path, target)
    except OSError:
        return None
    return target


def _reclaim_stale(queue, *, stale_seconds: float = 300.0) -> list[Path]:
    """把超时未完成的 .working 请求放回待处理，避免 worker 崩溃后丢请求。"""
    folder = Path(queue) / REQUESTS
    restored = []
    if not folder.is_dir():
        return restored
    now = time.time()
    for path in folder.glob("*.json.working"):
        try:
            if now - path.stat().st_mtime <= stale_seconds:
                continue
            target = path.with_suffix("")
            os.replace(path, target)
            restored.append(target)
        except OSError:
            continue
    return restored


def spawn_worker(script_path, *, args=("worker",)) -> bool:
    """派生一个脱离当前进程的 worker，让命名不依赖外部计划任务。

    Hook 必须秒回，所以这里只负责拉起；子进程使用 DEVNULL 标准流，绝不继承
    Hook 的 stdin/stdout 管道（否则宿主会一直等管道关闭）。失败不影响 Hook 契约，
    请求仍留在队列里，可由计划任务等外部触发器消费。
    """
    try:
        command = [sys.executable, "-X", "utf8", str(script_path), *args]
        options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                   "stderr": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        else:
            options["start_new_session"] = True
        subprocess.Popen(command, **options)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def run_worker(queue, process_one, *, max_attempts: int = MAX_ATTEMPTS, limit: int | None = None,
               stale_seconds: float = 300.0) -> dict:
    """用户身份执行：取走队列中的请求并交给 ``process_one``。

    ``process_one(payload)`` 复用 Hook 的命名流程，返回带 ``status`` 的字典；
    抛异常时按 ``attempts`` 重试，直到 ``max_attempts`` 后落入 failed/。
    """
    queue = Path(queue)
    stats = {"processed": 0, "renamed": 0, "kept": 0, "skipped": 0, "failed": 0}
    _reclaim_stale(queue, stale_seconds=stale_seconds)
    for path in pending(queue):
        if limit is not None and stats["processed"] >= limit:
            break
        claimed = _claim(path)
        if claimed is None:
            continue
        payload = read_request(claimed)
        thread_id = payload.get("thread_id")
        if not thread_id:
            stats["failed"] += 1
            _archive(claimed, queue / FAILED, {**payload, "last_error": "缺少 thread_id"})
            log(queue, "丢弃无效请求：缺少 thread_id")
            continue
        try:
            result = process_one(payload) or {}
        except Exception as exc:  # 单条失败不影响后续请求
            stats["failed"] += 1
            record_failure(claimed, f"{type(exc).__name__}: {exc}", max_attempts=max_attempts)
            log(queue, f"失败 {thread_id}：{type(exc).__name__}")
            continue
        stats["processed"] += 1
        status = result.get("status", "done")
        stats[status if status in ("renamed", "kept") else "skipped"] += 1
        _archive(claimed, queue / DONE, {**payload, "result": result, "finished_at": time.time()})
    return stats
