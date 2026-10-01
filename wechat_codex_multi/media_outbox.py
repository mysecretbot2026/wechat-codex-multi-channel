import hashlib
import contextlib
import fcntl
import json
import os
import time
import uuid
from pathlib import Path

from .actions import kind_for_path, normalize_media_path


def media_outbox_path(state_dir, conversation_key):
    digest = hashlib.sha256(str(conversation_key or "").encode("utf-8")).hexdigest()
    return Path(state_dir).expanduser().resolve() / "media_outbox" / f"{digest}.jsonl"


def queue_media(outbox_path, paths, kind=""):
    target = Path(outbox_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    written = []
    with _outbox_lock(target), target.open("a", encoding="utf-8") as fh:
        for raw in paths:
            normalized = normalize_media_path(raw)
            if not normalized:
                raise RuntimeError(f"无效媒体路径: {raw}")
            path = Path(normalized).expanduser().resolve()
            if not path.exists() or not path.is_file():
                raise RuntimeError(f"媒体文件不存在: {path}")
            action = {
                "id": uuid.uuid4().hex,
                "kind": kind or kind_for_path(str(path)),
                "path": str(path),
                "queuedAt": int(time.time() * 1000),
            }
            fh.write(json.dumps(action, ensure_ascii=False) + "\n")
            written.append(action)
    return written


@contextlib.contextmanager
def _outbox_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read_records(path):
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
            path_value = normalize_media_path(item.get("path") or "")
            if not path_value:
                continue
            records.append({"id": item.get("id") or hashlib.sha256(line.encode()).hexdigest(),
                            "kind": item.get("kind") or kind_for_path(path_value), "path": path_value})
        except (ValueError, TypeError, AttributeError):
            continue
    return records


def read_media_outbox(outbox_path):
    """Read pending actions without removing them before a successful send."""
    path = Path(outbox_path).expanduser().resolve()
    with _outbox_lock(path):
        return _read_records(path)


def acknowledge_media_outbox(outbox_path, action_ids):
    path = Path(outbox_path).expanduser().resolve()
    acknowledged = set(action_ids)
    with _outbox_lock(path):
        remaining = [item for item in _read_records(path) if item["id"] not in acknowledged]
        if not remaining:
            path.unlink(missing_ok=True)
            return
        temporary = path.with_suffix(path.suffix + ".tmp-" + uuid.uuid4().hex)
        try:
            temporary.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in remaining),
                                 encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def read_and_clear_media_outbox(outbox_path):
    """Compatibility helper; delivery code uses read + acknowledge instead."""
    actions = read_media_outbox(outbox_path)
    acknowledge_media_outbox(outbox_path, [item["id"] for item in actions])
    return [{"kind": item["kind"], "path": item["path"]} for item in actions]
