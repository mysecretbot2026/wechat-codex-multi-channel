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
            con.commit()
            con.close()

            sessions = list_codex_sessions({"name": "main", "codexHome": str(home)})

            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["agent"], "codex")
            self.assertEqual(sessions[0]["account"], "main")
            self.assertEqual(sessions[0]["sessionId"], "thread-1")
            self.assertEqual(sessions[0]["title"], "检查 xx_gg 目录")
            self.assertEqual(sessions[0]["cwd"], "/tmp/project")

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


if __name__ == "__main__":
    unittest.main()
