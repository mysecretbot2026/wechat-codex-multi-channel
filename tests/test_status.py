import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_desktop_codex import FakeDesktop, make_config
from test_service_performance import FakeRunRunner
from wechat_codex_multi.service import MultiWechatCodexService
from wechat_codex_multi.session_discovery import read_codex_session_title


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.backup = self.home / "backup"
        self.backup.mkdir()
        config = make_config(self.tmp.name)
        config["codex"]["accounts"].append({"name": "backup", "codexHome": str(self.backup)})
        self.service = MultiWechatCodexService(config)
        self.service.codex = FakeRunRunner()
        self.desktop = FakeDesktop()
        self.service.desktop = self.desktop
        self.account = {"accountId": "bot-1"}
        self.key = "bot-1:user-1"
        self.sent = []
        self.service._send_text = lambda account, user, text: self.sent.append(text)

    def tearDown(self):
        self.service.stop()
        self.tmp.cleanup()

    def command(self, text):
        self.service._handle_message_safe(self.account, "user-1", self.key, text)
        return self.sent[-1]

    def title_db(self, home, session_id, title):
        with sqlite3.connect(str(home / "state_5.sqlite")) as con:
            con.execute("create table threads (id text primary key, title text)")
            con.execute("insert into threads values (?, ?)", (session_id, title))
            con.executemany("insert into threads values (?, ?)", [(f"other-{i}", "其他会话") for i in range(50)])

    def select_desktop(self):
        self.command("/d-sessions all")
        self.command("/d-session 1")

    def test_cli_title_uses_current_account_and_id_and_reads_renames(self):
        self.title_db(self.home, "selected", "主账号的同名 ID")
        self.title_db(self.backup, "selected", "用户消息：修复 登录\n问题")
        self.service.config["codex"]["runner"] = "app-server"
        self.service.state.update_session(self.key, agent="codex", codexAccount="backup", codexThreadId="selected")
        with patch.object(self.service.codex, "run") as run, patch.object(self.desktop, "read") as read:
            status = self.command("/状态")
            self.assertIn("当前对话：修复 登录 问题", status)
            self.assertIn("会话来源：Codex CLI", status)
            self.assertIn("会话账号：backup", status)
            self.assertIn("sessionId: selected", status)
            self.assertNotIn("主账号的同名 ID", status)
            with sqlite3.connect(str(self.backup / "state_5.sqlite")) as con:
                con.execute("update threads set title = '重新命名的会话' where id = 'selected'")
            self.assertIn("当前对话：重新命名的会话", self.command("/status"))
            run.assert_not_called()
            read.assert_not_called()
        self.command("/codex main")
        status = self.command("/status")
        self.assertIn("当前对话：新会话（尚未创建）", status)
        self.assertIn("会话账号：main", status)
        self.assertNotIn("重新命名", status)

    def test_desktop_title_reuses_native_read_and_ignores_browsing_account(self):
        self.select_desktop()
        self.command("/d-account backup")
        native = {"id": self.desktop.thread["id"], "name": "刚改过的桌面标题",
                  "model": "gpt-native", "reasoningEffort": "xhigh"}
        with patch.object(self.desktop, "read", return_value=native) as read, \
                patch.object(self.service.codex, "run") as run:
            status = self.command("/status")
            self.assertIn("当前对话：刚改过的桌面标题", status)
            self.assertIn("会话来源：Codex Desktop（桌面）", status)
            self.assertIn("会话账号：main", status)
            self.assertIn("codexModel: gpt-native", status)
            self.assertIn("codexThreadId: " + self.desktop.thread["id"], status)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(read.call_args.args[0]["name"], "main")
            self.assertEqual(read.call_args.args[1], self.desktop.thread["id"])
            run.assert_not_called()

    def test_desktop_read_failure_keeps_status_and_uses_local_title(self):
        self.select_desktop()
        native_db = self.home / "sqlite" / "codex-dev.db"
        native_db.parent.mkdir()
        with sqlite3.connect(str(native_db)) as con:
            con.execute("create table local_thread_catalog (thread_id text, display_title text, source_kind text)")
            con.execute("insert into local_thread_catalog values (?, ?, ?)",
                        (self.desktop.thread["id"], "本地桌面标题", "codex"))
        with patch.object(self.desktop, "read", side_effect=RuntimeError("metadata unavailable")):
            status = self.command("/status")
        self.assertIn("当前对话：本地桌面标题", status)
        self.assertIn("会话账号：main", status)
        self.assertIn("modelWarning:", status)
        self.assertNotIn("命令执行失败", status)

    def test_cache_title_is_scoped_to_selected_agent_account_and_session(self):
        self.service.state.update_session(self.key, agent="codex", codexAccount="backup", codexThreadId="cached-id")
        self.service.session_selection_cache[self.key] = [
            {"agent": "claude", "account": "backup", "sessionId": "cached-id", "title": "其他 Agent"},
            {"agent": "codex", "account": "main", "sessionId": "cached-id", "title": "其他账号"},
            {"agent": "codex", "account": "backup", "sessionId": "cached-id", "title": "当前标题"},
        ]
        with patch("wechat_codex_multi.service.read_codex_session_title", side_effect=PermissionError("unreadable")):
            status = self.command("/status")
        self.assertIn("当前对话：当前标题（本地列表缓存）", status)
        self.assertNotIn("其他账号", status)
        self.service.state.update_session(self.key, codexThreadId="unavailable")
        self.assertIn("当前对话：标题暂不可用", self.command("/status"))
        self.command("/reset")
        self.assertIn("当前对话：新会话（尚未创建）", self.command("/status"))

    def test_new_desktop_conversation_does_not_borrow_previous_title(self):
        self.select_desktop()
        self.command("/codex backup")
        with patch.object(self.desktop, "read") as read:
            status = self.command("/status")
            self.assertIn("当前对话：新会话（尚未创建）", status)
            self.assertIn("会话来源：Codex Desktop（桌面）", status)
            self.assertIn("会话账号：backup", status)
            self.assertNotIn(self.desktop.thread["title"], status)
            read.assert_not_called()

    def test_claude_selection_is_not_mistaken_for_previous_desktop_thread(self):
        self.select_desktop()
        self.command("/agent claude")
        self.service.state.update_session(self.key, claudeSessionId="claude-current")
        project = self.home / "projects" / "example"
        project.mkdir(parents=True)
        (project / "claude-current.jsonl").write_text(json.dumps({
            "type": "user", "message": {"content": [{"type": "text", "text": "当前 Claude 要求"}]},
        }), encoding="utf-8")
        with patch("wechat_codex_multi.service.read_claude_auth_status", return_value={"loggedIn": False}), \
                patch.object(self.desktop, "read") as read:
            status = self.command("/status")
            self.assertIn("当前对话：当前 Claude 要求", status)
            self.assertIn("会话来源：Claude CLI", status)
            self.assertIn("sessionId: claude-current", status)
            self.assertIn("agent: claude", status)
            read.assert_not_called()

    def test_missing_corrupt_and_legacy_metadata_are_safe_and_read_only(self):
        account = {"name": "backup", "codexHome": str(self.backup)}
        self.assertEqual(read_codex_session_title(account, "selected", desktop=True), "")
        self.assertEqual(list(self.backup.iterdir()), [])
        (self.backup / "state_5.sqlite").write_text("not sqlite")
        (self.backup / "session_index.jsonl").write_text(
            "\n".join(["bad json", "null", json.dumps({"id": "other", "thread_name": "不要显示"}),
                       json.dumps({"id": "selected", "thread_name": "初始标题"}),
                       json.dumps({"id": "selected", "thread_name": "更新后的标题"})]), encoding="utf-8")
        self.assertEqual(read_codex_session_title(account, "selected", desktop=True), "更新后的标题")
        self.assertEqual((self.backup / "state_5.sqlite").read_text(), "not sqlite")
        self.title_db(self.home, "selected", "旧版数据库标题")
        self.assertEqual(read_codex_session_title({"codexHome": str(self.home)}, "selected"), "旧版数据库标题")


if __name__ == "__main__":
    unittest.main()
