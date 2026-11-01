"""从宿主会话记录读取最后用户消息时间，独立维护标题时间后缀。"""
from datetime import datetime, timezone
import json
import math
from pathlib import Path

MAX_SCAN_BYTES = 16 * 1024 * 1024


def _timestamp(value):
    if type(value) in (int, float):
        try:
            finite = math.isfinite(value)
        except OverflowError:
            return None
        if not finite or value <= 0:
            return None
        stamp = value / 1000 if value >= 100_000_000_000 else value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            stamp = parsed.timestamp()
        except (ValueError, OverflowError):
            return None
    else:
        return None
    try:
        parsed = datetime.fromtimestamp(stamp, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return stamp if 2000 <= parsed.year <= 2100 else None


def _reverse_lines(path):
    """只回读有限尾部；超长或损坏记录不阻塞正常命名。"""
    with path.open("rb") as stream:
        position = stream.seek(0, 2)
        lower = max(0, position - MAX_SCAN_BYTES)
        pending = b""
        while position > lower:
            size = min(65536, position - lower)
            position -= size
            stream.seek(position)
            chunks = (stream.read(size) + pending).split(b"\n")
            pending = chunks[0]
            for line in reversed(chunks[1:]):
                if line:
                    yield line
        if lower == 0 and pending:
            yield pending


def _text(parts):
    if not isinstance(parts, list):
        return ""
    return "\n".join(part.get("text", "") for part in parts if isinstance(part, dict)
                     and part.get("type") in ("text", "input_text") and isinstance(part.get("text", ""), str))


def last_user_timestamp(thread):
    """匹配最新用户消息与轮次；不以线程更新时间或助手完成时间代替。"""
    turn, message = None, None
    for candidate in reversed(thread.get("turns", [])):
        message = next((item for item in reversed(candidate.get("items", []))
                        if item.get("type") == "userMessage"), None)
        if message:
            turn = candidate
            break
    if message is None:
        return None
    for key in ("createdAt", "timestamp"):
        stamp = _timestamp(message.get(key))
        if stamp is not None:
            return stamp
    raw_path = thread.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = Path(raw_path)
    if path.suffix.lower() != ".jsonl":
        return None
    wanted_text = _text(message.get("content", []))
    candidate_stamp = None
    try:
        for line in _reverse_lines(path):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict):
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            # CLI 的旧记录没有消息级轮次信息；用前面的轮次开始记录复核归属。
            boundary = ((row.get("type") == "turn_context") or
                        (row.get("type") == "event_msg" and payload.get("type") == "task_started"))
            if candidate_stamp is not None and boundary and payload.get("turn_id"):
                return candidate_stamp if payload["turn_id"] == turn.get("id") else None
            if row.get("type") != "response_item" or payload.get("type") != "message" or payload.get("role") != "user":
                continue
            if _text(payload.get("content", [])) != wanted_text:
                continue
            metadata = payload.get("internal_chat_message_metadata_passthrough")
            metadata = metadata if isinstance(metadata, dict) else {}
            if metadata.get("turn_id") and metadata["turn_id"] != turn.get("id"):
                continue
            stamp = _timestamp(metadata.get("create_time")) or _timestamp(row.get("timestamp"))
            if stamp is None:
                continue
            if metadata.get("turn_id") == turn.get("id"):
                return stamp
            if candidate_stamp is None:
                candidate_stamp = stamp
    except OSError:
        pass
    return None


def managed_base_title(state, title):
    """只剥离本程序已记录的后缀，用户自己写入的日期属于标题正文。"""
    if title == state.get("pending_title") and isinstance(state.get("pending_base_title"), str):
        return state["pending_base_title"]
    if title == state.get("last_seen_title") and isinstance(state.get("last_base_title"), str):
        return state["last_base_title"]
    return title


def display_title(base, thread, enabled=True, *, tz=None):
    if not enabled or not base:
        return base
    stamp = last_user_timestamp(thread)
    if stamp is None:
        return base
    clock = datetime.fromtimestamp(stamp, timezone.utc).astimezone(tz)
    return base + " · " + clock.strftime("%m-%d %H:%M")
