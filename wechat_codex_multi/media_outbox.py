import hashlib
import json
import os
import time
from pathlib import Path

from .actions import kind_for_path, normalize_media_path


def media_outbox_path(state_dir, conversation_key):
    digest = hashlib.sha256(str(conversation_key or "").encode("utf-8")).hexdigest()
    return Path(state_dir).expanduser().resolve() / "media_outbox" / f"{digest}.jsonl"


def queue_media(outbox_path, paths, kind=""):
    target = Path(outbox_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    written = []
    with target.open("a", encoding="utf-8") as fh:
        for raw in paths:
            normalized = normalize_media_path(raw)
            if not normalized:
                raise RuntimeError(f"无效媒体路径: {raw}")
            path = Path(normalized).expanduser().resolve()
            if not path.exists() or not path.is_file():
                raise RuntimeError(f"媒体文件不存在: {path}")
            action = {
                "kind": kind or kind_for_path(str(path)),
                "path": str(path),
                "queuedAt": int(time.time() * 1000),
            }
            fh.write(json.dumps(action, ensure_ascii=False) + "\n")
            written.append(action)
    return written


def read_and_clear_media_outbox(outbox_path):
    path = Path(outbox_path).expanduser().resolve()
    if not path.exists():
        return []
    processing = path.with_suffix(path.suffix + f".{os.getpid()}.processing")
    try:
        os.replace(path, processing)
    except FileNotFoundError:
        return []
    actions = []
    try:
        for line in processing.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
            except Exception:
                continue
            kind = item.get("kind") or kind_for_path(item.get("path") or "")
            path_value = normalize_media_path(item.get("path") or "")
            if not path_value:
                continue
            actions.append({"kind": kind, "path": path_value})
    finally:
        try:
            processing.unlink()
        except FileNotFoundError:
            pass
    return actions
