"""Durable, user-scoped task results and delivery receipts."""

import copy
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path


TASK_LABELS = {
    "queued": "排队中", "running": "执行中", "completed": "已完成", "failed": "失败",
    "cancelled": "已取消", "interrupted": "服务重启，执行结果未确认",
}
ROUTING_FIELDS = ("cwd", "agent", "codexClient", "codexAccount", "claudeAccount",
                  "codexThreadId", "claudeSessionId")


class TaskJournal:
    def __init__(self, state_dir):
        self.directory = Path(state_dir) / "tasks"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.records = {}
        for path in self.directory.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(record, dict):
                    continue
                if not re.fullmatch(r"[a-f0-9]{12}", record.get("id", "")) or record["id"] != path.stem:
                    continue
                self.records[record["id"]] = record
                if record.get("status") in {"queued", "running"}:
                    self.update(record["id"], status="interrupted", finishedAt=time.time(),
                                error="服务已重启；保留任务记录，不自动重新执行。")
            except (OSError, ValueError, TypeError):
                continue

    def _save(self, record):
        target = self.directory / (record["id"] + ".json")
        temporary = target.with_suffix(".tmp-" + uuid.uuid4().hex)
        try:
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def create(self, owner, conversation_key, session, prompt, workspace="default"):
        with self.lock:
            task_id = uuid.uuid4().hex[:12]
            record = {
                "id": task_id, "owner": owner, "conversationKey": conversation_key,
                "workspace": workspace, "cwd": session.get("cwd") or "",
                "agent": session.get("agent") or "codex", "client": session.get("codexClient") or "cli",
                "account": session.get("claudeAccount") if session.get("agent") == "claude" else session.get("codexAccount"),
                "sessionId": session.get("claudeSessionId") if session.get("agent") == "claude" else session.get("codexThreadId"),
                "routing": {key: copy.deepcopy(session.get(key)) for key in ROUTING_FIELDS},
                "title": " ".join(str(prompt or "").split())[:100] or "媒体任务",
                "prompt": str(prompt or ""),
                "status": "queued", "receivedAt": time.time(), "startedAt": None, "finishedAt": None,
                "chunks": [], "media": [], "hasOutput": False, "unread": False,
                "background": False, "notified": False, "deliveryError": "", "error": "",
            }
            self._save(record)
            self.records[task_id] = record
            return copy.deepcopy(record)

    def get(self, task_id):
        with self.lock:
            return copy.deepcopy(self.records.get(task_id))

    def update(self, task_id, **updates):
        with self.lock:
            record = copy.deepcopy(self.records[task_id])
            record.update(updates)
            self._save(record)
            self.records[task_id] = record
            return copy.deepcopy(record)

    def list(self, owner, limit=20):
        with self.lock:
            records = [item for item in self.records.values() if item.get("owner") == owner]
            records.sort(key=lambda item: item.get("receivedAt", 0), reverse=True)
            return copy.deepcopy(records[:limit] if limit is not None else records)

    def find(self, owner, selector="", output_only=False):
        records = self.list(owner, limit=None)
        if output_only:
            records = [item for item in records if item.get("hasOutput")]
        value = str(selector or "").strip().lower()
        if value in {"", "latest", "最近"}:
            return records[0] if records else None
        matches = [item for item in records if item["id"].startswith(value)]
        return matches[0] if len(matches) == 1 else None

    def active(self):
        with self.lock:
            return copy.deepcopy([item for item in self.records.values()
                                  if item.get("status") in {"queued", "running"}])

    def claimed_media_ids(self, conversation_key):
        with self.lock:
            return {action_id for record in self.records.values()
                    if record.get("conversationKey") == conversation_key
                    for item in record.get("media", []) for action_id in item.get("outboxIds", [])}


def task_elapsed(record):
    start = record.get("startedAt") or record.get("receivedAt") or time.time()
    seconds = max(0, int((record.get("finishedAt") or time.time()) - start))
    return f"{seconds // 60} 分 {seconds % 60} 秒" if seconds >= 60 else f"{seconds} 秒"
