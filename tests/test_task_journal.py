import concurrent.futures
import copy
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_codex_multi.cli import main
from wechat_codex_multi.task_journal import TaskJournal


class TaskJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.owner = "bot:user"

    def journal(self, **kwargs):
        journal = TaskJournal(self.root, **kwargs)
        self.addCleanup(journal.close)
        return journal

    def record(self, task_id="abcdef123456", **updates):
        record = {
            "id": task_id, "owner": self.owner, "conversationKey": self.owner + ":project",
            "workspace": "项目一", "cwd": "/tmp/项目", "agent": "codex", "client": "cli",
            "account": "main", "sessionId": "session-1", "routing": {"codexAccount": "main"},
            "title": "历史任务", "prompt": "完整指令\n第二行", "status": "completed",
            "receivedAt": 123.25, "startedAt": 124.5, "finishedAt": 125.75,
            "chunks": [{"text": "第一段", "sent": True}, {"text": "第二段", "sent": False}],
            "media": [{"path": "/tmp/文件.pdf", "sent": False, "outboxIds": ["media-1"]}],
            "hasOutput": True, "unread": True, "background": True, "notified": False,
            "deliveryError": "网络中断", "error": "", "futureField": {"nested": [1, None, "中文"]},
            "handoffRecords": [{"role": "assistant", "text": "历史交接内容"}],
        }
        record.update(updates)
        return record

    def legacy(self, record):
        directory = self.root / "tasks"
        directory.mkdir(exist_ok=True)
        path = directory / (record["id"] + ".json")
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def database_records(self):
        connection = sqlite3.connect(self.root / "tasks.sqlite3")
        self.addCleanup(connection.close)
        return {task_id: json.loads(raw) for task_id, raw in connection.execute(
            "SELECT id, record_json FROM tasks"
        )}

    def test_create_update_and_reload_use_sqlite_without_new_json_files(self):
        journal = self.journal()
        record = journal.create(self.owner, self.owner + ":project", {"codexAccount": "main"}, "运行任务")
        expected = journal.update(record["id"], status="completed", chunks=[{"text": "结果", "sent": True}],
                                  hasOutput=True, unread=False, custom={"value": "完整保存"})
        record["routing"]["codexAccount"] = "changed"
        self.assertEqual(journal.get(record["id"]), expected)
        self.assertEqual(list((self.root / "tasks").glob("*.json")), [])
        journal.close()
        reloaded = self.journal()
        self.assertEqual(reloaded.get(record["id"]), expected)
        self.assertFalse(hasattr(reloaded, "records"))

    def test_staged_migration_preserves_all_fields_and_execution_states(self):
        originals = []
        for index, status in enumerate(("completed", "failed", "cancelled", "queued", "running")):
            record = self.record(f"{index + 1:012x}", status=status)
            path = self.legacy(record)
            originals.append((record, path, path.read_bytes()))
        report = TaskJournal.migrate_legacy(self.root)
        self.assertEqual(report["importedRecords"], 5)
        self.assertEqual(report["totalRecords"], 5)
        self.assertEqual(report["integrity"], "ok")
        self.assertFalse(report["migrationComplete"])
        self.assertEqual(report["skippedFiles"], [])
        migrated = self.database_records()
        for record, path, raw in originals:
            with self.subTest(status=record["status"]):
                self.assertEqual(migrated[record["id"]], record)
                self.assertEqual(path.read_bytes(), raw)

    def test_first_start_resyncs_late_json_and_later_starts_ignore_stale_backups(self):
        running = self.record(status="running", hasOutput=False, chunks=[], media=[])
        self.legacy(running)
        TaskJournal.migrate_legacy(self.root)
        completed = self.record()
        self.legacy(completed)
        extra = self.record("123456abcdef", title="预迁移后收到的新任务")
        self.legacy(extra)
        journal = self.journal()
        self.assertEqual(journal.get(completed["id"]), completed)
        self.assertEqual(journal.get(extra["id"]), extra)
        chunks = copy.deepcopy(completed["chunks"])
        chunks[1]["sent"] = True
        updated = journal.update(completed["id"], chunks=chunks, unread=False, deliveryError="")
        journal.close()
        self.legacy(running)
        self.legacy(self.record("999999999999", title="切换后留下的旧文件"))
        reloaded = self.journal()
        self.assertEqual(reloaded.get(completed["id"]), updated)
        self.assertIsNone(reloaded.get("999999999999"))
        report = TaskJournal.migrate_legacy(self.root)
        self.assertTrue(report["migrationComplete"])
        self.assertEqual(report["importedRecords"], 0)
        self.assertEqual(report["totalRecords"], 2)
        self.assertEqual(reloaded.get(completed["id"]), updated)

    def test_repeated_staging_refreshes_records_without_duplicates_or_interrupts(self):
        record = self.record(status="running")
        self.legacy(record)
        first = TaskJournal.migrate_legacy(self.root)
        record["title"] = "旧服务最后更新的标题"
        self.legacy(record)
        second = TaskJournal.migrate_legacy(self.root)
        self.assertEqual(first["totalRecords"], second["totalRecords"])
        self.assertEqual(self.database_records()[record["id"]], record)
        self.assertFalse(second["migrationComplete"])

    def test_bad_json_is_reported_and_original_files_are_retained(self):
        self.legacy(self.record())
        directory = self.root / "tasks"
        bad_files = {
            "111111111111.json": "{incomplete",
            "222222222222.json": "[]",
            "333333333333.json": json.dumps(self.record("444444444444")),
            "555555555555.json": json.dumps({"id": ["555555555555"]}),
        }
        for name, raw in bad_files.items():
            (directory / name).write_text(raw)
        with patch("wechat_codex_multi.task_journal.log.warn") as warn:
            report = TaskJournal.migrate_legacy(self.root)
        self.assertEqual(report["importedRecords"], 1)
        self.assertEqual(report["sourceFiles"], 5)
        self.assertEqual(set(report["skippedFiles"]), set(bad_files))
        self.assertEqual(warn.call_count, 4)
        for name, raw in bad_files.items():
            self.assertEqual((directory / name).read_text(), raw)

    def test_failed_import_rolls_back_all_rows_and_can_be_retried(self):
        self.legacy(self.record("111111111111"))
        self.legacy(self.record("222222222222"))
        original_save = TaskJournal._save

        def fail_second(journal, record):
            original_save(journal, record)
            if record["id"] == "222222222222":
                raise RuntimeError("模拟磁盘写入失败")

        with patch.object(TaskJournal, "_save", autospec=True, side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "模拟磁盘"):
                TaskJournal.migrate_legacy(self.root)
        self.assertEqual(self.database_records(), {})
        connection = sqlite3.connect(self.root / "tasks.sqlite3")
        self.addCleanup(connection.close)
        self.assertIsNone(connection.execute(
            "SELECT value FROM task_journal_meta WHERE key='legacy_json_migration'"
        ).fetchone())
        self.assertEqual(TaskJournal.migrate_legacy(self.root)["totalRecords"], 2)

    def test_unreadable_legacy_file_aborts_instead_of_finalizing_a_partial_import(self):
        self.legacy(self.record("111111111111"))
        target = self.legacy(self.record("222222222222"))
        original_read = Path.read_text

        def unreadable(path, *args, **kwargs):
            if path == target:
                raise OSError("模拟读取失败")
            return original_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", autospec=True, side_effect=unreadable):
            with self.assertRaises(OSError):
                self.journal()
        self.assertEqual(self.database_records(), {})
        self.assertEqual(len(self.journal().list(self.owner)), 2)

    def test_restart_interrupts_only_unfinished_tasks_and_preserves_partial_outputs(self):
        records = [self.record(f"{index + 1:012x}", status=status)
                   for index, status in enumerate(("queued", "running", "completed", "cancelled", "failed"))]
        for record in records:
            self.legacy(record)
        journal = self.journal()
        for record in records:
            current = journal.get(record["id"])
            if record["status"] in {"queued", "running"}:
                self.assertEqual(current["status"], "interrupted")
                self.assertIn("不自动重新执行", current["error"])
                self.assertEqual(current["chunks"], record["chunks"])
                self.assertEqual(current["media"], record["media"])
            else:
                self.assertEqual(current, record)
        self.assertEqual(journal.active(), [])

    def test_listing_filters_owner_status_and_unread_in_sql_and_supports_pagination(self):
        journal = self.journal()
        created = []
        for index in range(15):
            record = journal.create(self.owner, self.owner, {}, str(index))
            created.append(journal.update(record["id"], receivedAt=index,
                                          status="running" if index % 2 else "completed", unread=bool(index % 3)))
        journal.create("other:user", "other:user", {}, "其他人的任务")
        ordered = list(reversed(created))
        self.assertEqual(journal.list(self.owner, limit=4, offset=3), ordered[3:7])
        self.assertEqual(journal.list(self.owner, limit=None, offset=10), ordered[10:])
        self.assertEqual(journal.list(self.owner, limit=0), [])
        self.assertEqual(journal.list(self.owner, statuses=()), [])
        self.assertEqual(journal.list(self.owner, limit=None, statuses=("running",), unread=True),
                         [record for record in ordered if record["status"] == "running" and record["unread"]])
        self.assertTrue(all(record["owner"] == self.owner for record in journal.list(self.owner, limit=None)))

    def test_find_prefix_ambiguity_owner_isolation_and_output_filtering(self):
        first = self.record("abcd00000001", receivedAt=10)
        second = self.record("abcd00000002", receivedAt=20, hasOutput=False)
        foreign = self.record("abcd00000003", owner="other:user", receivedAt=30)
        for record in (first, second, foreign):
            self.legacy(record)
        journal = self.journal()
        self.assertEqual(journal.find(self.owner), second)
        self.assertEqual(journal.find(self.owner, "最近", output_only=True), first)
        self.assertIsNone(journal.find(self.owner, "abcd"))
        self.assertEqual(journal.find(self.owner, "ABCD", output_only=True), first)
        self.assertEqual(journal.find(self.owner, first["id"]), first)
        self.assertIsNone(journal.find(self.owner, foreign["id"]))
        self.assertIsNone(journal.find(self.owner, "%"))

    def test_updates_refresh_retry_notification_and_active_queries(self):
        journal = self.journal()
        task = journal.create(self.owner, self.owner, {}, "任务")
        other = journal.create("other:user", "other:user", {}, "其他用户")
        journal.update(other["id"], status="completed", hasOutput=True, deliveryError="离线", background=True)
        self.assertEqual({record["id"] for record in journal.active()}, {task["id"]})
        self.assertIsNone(journal.pending_delivery(self.owner))
        self.assertIsNone(journal.pending_notification(self.owner))
        updated = journal.update(task["id"], status="failed", background=True, deliveryError="离线", hasOutput=True)
        self.assertEqual(journal.pending_delivery(self.owner), updated)
        self.assertEqual(journal.pending_notification(self.owner), updated)
        journal.update(task["id"], deliveryError="", notified=True)
        self.assertIsNone(journal.pending_delivery(self.owner))
        self.assertIsNone(journal.pending_notification(self.owner))
        self.assertEqual(journal.active(), [])

    def test_media_claims_are_scoped_to_conversation_and_survive_reload(self):
        record = self.record()
        self.legacy(record)
        self.legacy(self.record("111111111111", conversationKey="other:conversation",
                                media=[{"outboxIds": ["foreign-media"]}]))
        journal = self.journal()
        self.assertEqual(journal.claimed_media_ids(record["conversationKey"]), {"media-1"})
        journal.update(record["id"], media=[{"outboxIds": ["media-1", "media-2"]}])
        journal.close()
        self.assertEqual(self.journal().claimed_media_ids(record["conversationKey"]), {"media-1", "media-2"})

    def test_failed_update_rolls_back_payload_and_query_columns(self):
        journal = self.journal()
        original = journal.create(self.owner, self.owner, {}, "原始任务")
        original_save = journal._save

        def fail_after_write(record):
            original_save(record)
            raise RuntimeError("提交前失败")

        with patch.object(journal, "_save", side_effect=fail_after_write):
            with self.assertRaises(RuntimeError):
                journal.update(original["id"], status="completed", unread=True, hasOutput=True, deliveryError="离线")
        self.assertEqual(journal.get(original["id"]), original)
        self.assertEqual(journal.list(self.owner, unread=True), [])
        self.assertIsNone(journal.pending_delivery(self.owner))
        journal.update(original["id"], title="失败后仍可更新")
        self.assertEqual(journal.get(original["id"])["title"], "失败后仍可更新")

    def test_concurrent_updates_across_threads_and_connections_preserve_distinct_fields(self):
        first = self.journal()
        task = first.create(self.owner, self.owner, {}, "并发任务")
        first.update(task["id"], status="completed")
        second = self.journal()

        def update_field(index):
            journal = first if index % 2 else second
            for value in range(10):
                journal.update(task["id"], **{f"worker{index}": value})
                journal.get(task["id"])
                journal.list(self.owner)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(update_field, range(8)))
        for index in range(8):
            self.assertEqual(first.get(task["id"])[f"worker{index}"], 9)
        self.assertEqual(first.get(task["id"]), second.get(task["id"]))

    def test_returned_records_do_not_mutate_persisted_nested_data(self):
        original = self.record()
        self.legacy(original)
        journal = self.journal()
        for record in (journal.get(original["id"]), journal.list(self.owner)[0], journal.find(self.owner)):
            record["media"][0]["sent"] = True
            record["futureField"]["nested"].append("changed")
        self.assertEqual(journal.get(original["id"]), original)

    def test_missing_update_and_immutable_id_do_not_change_other_tasks(self):
        journal = self.journal()
        original = journal.create(self.owner, self.owner, {}, "任务")
        with self.assertRaises(KeyError):
            journal.update("111111111111", title="不存在")
        with self.assertRaises(ValueError):
            journal.update(original["id"], id="222222222222")
        self.assertIsNone(journal.get("222222222222"))
        self.assertEqual(journal.get(original["id"]), original)

    def test_migration_cli_does_not_start_service_or_mark_active_tasks_interrupted(self):
        record = self.record(status="running")
        self.legacy(record)
        config = self.root / "config.json"
        config.write_text(json.dumps({"stateDir": str(self.root)}))
        stdout = io.StringIO()
        with patch("sys.stdout", stdout), patch("wechat_codex_multi.cli.MultiWechatCodexService") as service:
            main(["--config", str(config), "migrate-tasks"])
        service.assert_not_called()
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["totalRecords"], 1)
        self.assertFalse(report["migrationComplete"])
        self.assertEqual(self.database_records()[record["id"]], record)

    def test_unknown_schema_version_is_rejected_without_overwriting_database(self):
        connection = sqlite3.connect(self.root / "tasks.sqlite3")
        connection.execute("PRAGMA user_version=99")
        connection.close()
        with self.assertRaisesRegex(RuntimeError, "不支持的任务数据库版本"):
            self.journal()
        connection = sqlite3.connect(self.root / "tasks.sqlite3")
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)


if __name__ == "__main__":
    unittest.main()
