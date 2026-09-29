import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from wechat_codex_multi.session_discovery import list_claude_sessions, list_codex_sessions


class SessionDiscoveryTests(unittest.TestCase):
    def test_list_codex_sessions_reads_threads_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            con = sqlite3.connect(home / "state_5.sqlite")
            con.execute(
                """
                create table threads (
                    id text primary key,
                    title text not null,
                    cwd text not null,
                    source text not null,
                    created_at integer not null,
                    updated_at integer not null,
                    archived integer not null
                )
                """
            )
            con.execute(
                "insert into threads values (?, ?, ?, ?, ?, ?, ?)",
                (
                    "thread-1",
                    "用户消息：检查 xx_gg 目录",
                    "/tmp/project",
                    "exec",
                    100,
                    200,
                    0,
                ),
            )
            con.executemany(
                "insert into threads values (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("desktop-1", "桌面会话", "/tmp/project", "vscode", 110, 300, 0),
                    ("app-server-1", "App Server 会话", "/tmp/project", "appServer", 120, 400, 0),
                ],
            )
            con.commit()
            con.close()

            sessions = list_codex_sessions({"name": "main", "codexHome": str(home)})

            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["agent"], "codex")
            self.assertEqual(sessions[0]["account"], "main")
            self.assertEqual(sessions[0]["sessionId"], "thread-1")
            self.assertEqual(sessions[0]["title"], "检查 xx_gg 目录")
            self.assertEqual(sessions[0]["cwd"], "/tmp/project")
            self.assertNotIn("desktop-1", [item["sessionId"] for item in sessions])
            self.assertNotIn("app-server-1", [item["sessionId"] for item in sessions])

            con = sqlite3.connect(str(home / "state_5.sqlite"))
            con.execute("update threads set archived = 1 where id = 'thread-1'")
            con.commit()
            con.close()
            self.assertEqual(list_codex_sessions({"name": "main", "codexHome": str(home)}), [])
            archived = list_codex_sessions({"name": "main", "codexHome": str(home)}, archived_only=True)
            self.assertEqual([item["sessionId"] for item in archived], ["thread-1"])

    def test_codex_index_fallback_keeps_only_cli_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            home.joinpath("session_index.jsonl").write_text(
                "\n".join([
                    json.dumps({"id": "cli-1", "thread_name": "CLI 任务",
                                "updated_at": "2026-05-20T00:00:00Z"}),
                    json.dumps({"id": "desktop-1", "thread_name": "桌面任务",
                                "updated_at": "2026-05-21T00:00:00Z"}),
                ]) + "\n",
                encoding="utf-8",
            )
            sessions_dir = home / "sessions" / "2026" / "05" / "20"
            sessions_dir.mkdir(parents=True)
            for session_id, source in (("cli-1", "cli"), ("desktop-1", "vscode")):
                sessions_dir.joinpath(f"rollout-{session_id}.jsonl").write_text(
                    json.dumps({
                        "type": "session_meta",
                        "payload": {"session_id": session_id, "source": source,
                                    "cwd": f"/tmp/{session_id}", "timestamp": "2026-05-19T00:00:00Z"},
                    }) + "\n",
                    encoding="utf-8",
                )

            sessions = list_codex_sessions({"name": "main", "codexHome": str(home)})

            self.assertEqual([item["sessionId"] for item in sessions], ["cli-1"])
            self.assertEqual(sessions[0]["source"], "cli")
            self.assertEqual(sessions[0]["cwd"], "/tmp/cli-1")

    def test_list_claude_sessions_reads_meta_and_project_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            meta_dir = base / "usage-data" / "session-meta"
            meta_dir.mkdir(parents=True)
            meta_dir.joinpath("session-1.json").write_text(
                json.dumps(
                    {
                        "session_id": "session-1",
                        "project_path": "/tmp/project",
                        "start_time": "2026-05-19T00:03:32.166Z",
                        "first_prompt": "No prompt",
                    }
                ),
                encoding="utf-8",
            )
            project_dir = base / "projects" / "-tmp-project"
            project_dir.mkdir(parents=True)
            project_dir.joinpath("session-1.jsonl").write_text(
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": "2026-05-19T00:04:00.000Z",
                        "cwd": "/tmp/project",
                        "sessionId": "session-1",
                        "message": {"role": "user", "content": "修复登录问题"},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            sessions = list_claude_sessions({"name": "main", "claudeConfigDir": str(base)})

            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["agent"], "claude")
            self.assertEqual(sessions[0]["account"], "main")
            self.assertEqual(sessions[0]["sessionId"], "session-1")
            self.assertEqual(sessions[0]["title"], "修复登录问题")
            self.assertEqual(sessions[0]["cwd"], "/tmp/project")

            archived = list_claude_sessions(
                {"name": "main", "claudeConfigDir": str(base)},
                archived_ids={"session-1"}, archived_only=True,
            )
            self.assertEqual([item["sessionId"] for item in archived], ["session-1"])
            self.assertEqual(list_claude_sessions(
                {"name": "main", "claudeConfigDir": str(base)}, archived_ids={"session-1"},
            ), [])


if __name__ == "__main__":
    unittest.main()
