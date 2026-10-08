"""Durable, user-scoped task results and delivery receipts."""

import copy
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from . import logging as log


TASK_LABELS = {
    "queued": "排队中", "running": "执行中", "completed": "已完成", "failed": "失败",
    "cancelled": "已取消", "interrupted": "服务重启，执行结果未确认",
}
ROUTING_FIELDS = ("cwd", "agent", "codexClient", "codexAccount", "claudeAccount",
                  "codexThreadId", "claudeSessionId")


class TaskJournal:
    """SQLite task storage; legacy JSON is imported once at the first start.

    A staged migration leaves the cutover pending so an old running service can
    finish writing JSON. The first normal start imports its final snapshot and
    permanently stops reading the legacy files.
    """

    def __init__(self, state_dir, *, recover_interrupted=True, finalize_migration=True):
        self.directory = Path(state_dir) / "tasks"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = Path(state_dir) / "tasks.sqlite3"
        self.lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._create_schema()
            self.migration_report = self._migrate_legacy(finalize_migration)
            if recover_interrupted:
                self._recover_interrupted()
        except Exception:
            self.close()
            raise

    def _create_schema(self):
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1}:
            raise RuntimeError(f"不支持的任务数据库版本: {version}")
        self._connection.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT NOT NULL PRIMARY KEY,
                owner TEXT NOT NULL,
                conversation_key TEXT NOT NULL,
                status TEXT NOT NULL,
                received_at REAL NOT NULL,
                has_output INTEGER NOT NULL,
                unread INTEGER NOT NULL,
                delivery_failed INTEGER NOT NULL,
                needs_notification INTEGER NOT NULL,
                record_json TEXT NOT NULL,
                CHECK(length(id) = 12 AND id NOT GLOB '*[^0-9a-f]*')
            );
            CREATE INDEX IF NOT EXISTS tasks_owner_received ON tasks(owner, received_at DESC);
            CREATE INDEX IF NOT EXISTS tasks_owner_status_received ON tasks(owner, status, received_at DESC);
            CREATE INDEX IF NOT EXISTS tasks_owner_unread_received ON tasks(owner, unread, received_at DESC);
            CREATE INDEX IF NOT EXISTS tasks_owner_output_received ON tasks(owner, has_output, received_at DESC);
            CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS tasks_conversation ON tasks(conversation_key);
            CREATE INDEX IF NOT EXISTS tasks_delivery ON tasks(owner, delivery_failed, has_output, received_at DESC);
            CREATE INDEX IF NOT EXISTS tasks_notification ON tasks(owner, needs_notification, received_at DESC);
            CREATE TABLE IF NOT EXISTS task_journal_meta (
                key TEXT NOT NULL PRIMARY KEY,
                value TEXT NOT NULL
            );
            PRAGMA user_version=1;
            COMMIT;
        """)

    @staticmethod
    def _parameters(record):
        return (
            record["id"], record.get("owner") or "", record.get("conversationKey") or "",
            record.get("status") or "", float(record.get("receivedAt") or 0),
            int(bool(record.get("hasOutput"))), int(bool(record.get("unread"))),
            int(bool(record.get("deliveryError"))),
            int(bool(record.get("background") and not record.get("notified")
                     and record.get("status") in {"completed", "failed"})),
            json.dumps(record, ensure_ascii=False, separators=(",", ":")),
        )

    def _save(self, record):
        self._connection.execute("""
            INSERT INTO tasks(id, owner, conversation_key, status, received_at, has_output,
                              unread, delivery_failed, needs_notification, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                owner=excluded.owner, conversation_key=excluded.conversation_key,
                status=excluded.status, received_at=excluded.received_at,
                has_output=excluded.has_output, unread=excluded.unread,
                delivery_failed=excluded.delivery_failed, needs_notification=excluded.needs_notification,
                record_json=excluded.record_json
        """, self._parameters(record))

    def _migrate_legacy(self, finalize):
        report = {"database": str(self.path), "legacyDirectory": str(self.directory),
                  "sourceFiles": 0, "importedRecords": 0, "skippedFiles": [], "migrationComplete": False}
        with self.lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            marker = self._connection.execute(
                "SELECT value FROM task_journal_meta WHERE key='legacy_json_migration'"
            ).fetchone()
            if marker and marker[0] == "complete":
                report["migrationComplete"] = True
                return report
            for path in sorted(self.directory.glob("*.json")):
                report["sourceFiles"] += 1
                # Files are kept untouched. Read failures abort the transaction;
                # malformed records are reported like the old JSON loader.
                raw = path.read_text(encoding="utf-8")
                try:
                    record = json.loads(raw)
                    if (not isinstance(record, dict)
                            or not re.fullmatch(r"[a-f0-9]{12}", record.get("id", ""))
                            or record["id"] != path.stem):
                        raise ValueError("任务 ID 与文件名不匹配")
                    self._save(record)
                except (ValueError, TypeError, sqlite3.InterfaceError, sqlite3.ProgrammingError):
                    report["skippedFiles"].append(path.name)
                    log.warn(f"task JSON migration skipped invalid record: {path.name}")
                    continue
                report["importedRecords"] += 1
            self._connection.execute(
                "INSERT INTO task_journal_meta(key, value) VALUES ('legacy_json_migration', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("complete" if finalize else "pending",),
            )
            report["migrationComplete"] = bool(finalize)
        if report["sourceFiles"]:
            log.info(f"task JSON migration imported={report['importedRecords']} "
                     f"skipped={len(report['skippedFiles'])} finalized={bool(finalize)}")
        return report

    @classmethod
    def migrate_legacy(cls, state_dir):
        """Stage a migration without interrupting tasks in the running service."""
        journal = cls(state_dir, recover_interrupted=False, finalize_migration=False)
        try:
            report = copy.deepcopy(journal.migration_report)
            report["totalRecords"] = journal._connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            report["integrity"] = journal._connection.execute("PRAGMA integrity_check").fetchone()[0]
            if report["integrity"] != "ok":
                raise RuntimeError("任务数据库完整性检查失败")
            return report
        finally:
            journal.close()

    def _recover_interrupted(self):
        with self.lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            rows = self._connection.execute(
                "SELECT record_json FROM tasks WHERE status IN ('queued', 'running')"
            ).fetchall()
            for row in rows:
                record = json.loads(row[0])
                record.update(status="interrupted", finishedAt=time.time(),
                              error="服务已重启；保留任务记录，不自动重新执行。")
                self._save(record)

    def close(self):
        with self.lock:
            self._connection.close()

    def __del__(self):
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()

    def create(self, owner, conversation_key, session, prompt, workspace="default"):
        with self.lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            task_id = uuid.uuid4().hex[:12]
            while self.get(task_id) is not None:
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
            return copy.deepcopy(record)

    def get(self, task_id):
        with self.lock:
            row = self._connection.execute("SELECT record_json FROM tasks WHERE id=?", (task_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def update(self, task_id, **updates):
        with self.lock, self._connection:
            # Lock the database before reading, including against other processes,
            # so concurrent partial updates never overwrite one another.
            self._connection.execute("BEGIN IMMEDIATE")
            record = self.get(task_id)
            if record is None:
                raise KeyError(task_id)
            if updates.get("id", task_id) != task_id:
                raise ValueError("任务 ID 不能修改")
            record.update(updates)
            self._save(record)
            return copy.deepcopy(record)

    def _select(self, where, parameters, limit=None, offset=0):
        sql = "SELECT record_json FROM tasks WHERE " + where + " ORDER BY received_at DESC, rowid ASC"
        parameters = list(parameters)
        if limit is not None or offset:
            sql += " LIMIT ? OFFSET ?"
            parameters.extend((limit if limit is not None else -1, offset))
        with self.lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list(self, owner, limit=20, *, statuses=None, unread=None, offset=0):
        conditions, parameters = ["owner=?"], [owner]
        if statuses is not None:
            statuses = tuple(statuses)
            if not statuses:
                return []
            conditions.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            parameters.extend(statuses)
        if unread is not None:
            conditions.append("unread=?")
            parameters.append(int(bool(unread)))
        return self._select(" AND ".join(conditions), parameters, limit, offset)

    def find(self, owner, selector="", output_only=False):
        where, parameters = "owner=?", [owner]
        if output_only:
            where += " AND has_output=1"
        value = str(selector or "").strip().lower()
        if value in {"", "latest", "最近"}:
            records = self._select(where, parameters, limit=1)
            return records[0] if records else None
        if not re.fullmatch(r"[a-f0-9]{1,12}", value):
            return None
        matches = self._select(where + " AND id LIKE ?", parameters + [value + "%"], limit=2)
        return matches[0] if len(matches) == 1 else None

    def active(self):
        return self._select("status IN ('queued', 'running')", [])

    def pending_delivery(self, owner):
        records = self._select("owner=? AND delivery_failed=1 AND has_output=1", [owner], limit=1)
        return records[0] if records else None

    def pending_notification(self, owner):
        records = self._select("owner=? AND needs_notification=1", [owner], limit=1)
        return records[0] if records else None

    def claimed_media_ids(self, conversation_key):
        records = self._select("conversation_key=?", [conversation_key])
        return {action_id for record in records
                for item in record.get("media", []) for action_id in item.get("outboxIds", [])}


def task_elapsed(record):
    start = record.get("startedAt") or record.get("receivedAt") or time.time()
    seconds = max(0, int((record.get("finishedAt") or time.time()) - start))
    return f"{seconds // 60} 分 {seconds % 60} 秒" if seconds >= 60 else f"{seconds} 秒"
