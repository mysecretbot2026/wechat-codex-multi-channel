import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from wechat_codex_multi.codex_app_server import AppTurnState
from wechat_codex_multi.desktop_codex import DesktopCodexCatalog, project_for_thread
from wechat_codex_multi.service import MultiWechatCodexService
from wechat_codex_multi.state import StateStore


class FakeServer:
    def __init__(self):
        self.requests = []

    def request(self, method, params=None, timeout_s=30):
        self.requests.append((method, params or {}))
        if method == "project/list":
            return {"data": [{"id": "p1", "name": "手机控制助手",
                              "roots": [{"path": "/tmp/project"}]}], "nextCursor": None}
        if method == "project/create":
            return {"project": {"id": "p2", "name": params["name"], "roots": params["roots"]}}
        if method == "thread/read":
            return {"thread": {"turns": [
                {"id": "old", "status": "completed", "items": [
                    {"type": "agentMessage", "phase": "final_answer", "text": "旧结果"},
                ]},
                {"id": "new", "status": "completed", "items": [
                    {"type": "agentMessage", "phase": "commentary", "text": "过程"},
                    {"type": "agentMessage", "phase": "final_answer", "text": "最新完整结果"},
                ]},
            ]}}
        if method == "thread/list":
            if (params or {}).get("cursor"):
                return {"data": [{"id": "thread-2", "cwd": "/tmp/b", "preview": "second",
                                  "updatedAt": 2, "status": {"type": "notLoaded"}}], "nextCursor": None}
            return {"data": [{"id": "thread-1", "cwd": "/tmp/a", "preview": "first",
                              "updatedAt": 3, "status": {"type": "notLoaded"}}], "nextCursor": "page-2"}
        return {}


class FakeRunner:
    def __init__(self):
        self.server = FakeServer()
        self.lock = threading.RLock()
        self.contexts = {}

    def _server_for_account(self, account):
        return self.server

    def request_for_account(self, account, method, params=None, timeout_s=30):
        return self.server.request(method, params, timeout_s)


class EmptyThreadRunner(FakeRunner):
    def request_for_account(self, account, method, params=None, timeout_s=30):
        raise RuntimeError("thread is not materialized yet; list_turns is not supported yet")


class FakeDesktop:
    def __init__(self):
        self.runner = FakeRunner()
        self.calls = []
        self.result_text = "最终回答"
        self.turn_id = "turn-latest"
        self.thread = {
            "id": "thread-1234567890", "title": "设计随行助手", "cwd": "/tmp/project",
            "projectId": "project-1",
            "updatedAt": 100, "lastTurnStatus": "completed", "runtimeStatus": "notLoaded",
            "archived": False,
        }

        self.project_list = [{"id": "project-1", "name": "手机控制助手", "roots": ["/tmp/project"]}]

    def projects(self, account):
        return [dict(project) for project in self.project_list]

    def create_project(self, account, name, cwd, idempotency_key):
        project = {"id": f"project-{len(self.project_list) + 1}", "name": name, "roots": [cwd]}
        self.project_list.append(project)
        self.calls.append(("project/create", name, cwd, idempotency_key))
        return project

    def threads(self, account, archived=False):
        return [dict(self.thread)] if self.thread["archived"] == archived else []

    def read(self, account, thread_id, include_turns=False):
        self.calls.append(("read", thread_id, include_turns))
        if include_turns:
            return {"id": thread_id, "turns": [{"id": "turn-old", "status": "completed", "items": [
                {"type": "agentMessage", "phase": "final_answer", "text": "旧回答"},
            ]}, {"id": "turn-latest", "status": self.thread["lastTurnStatus"], "items": [
                {"type": "userMessage", "content": [{"type": "text", "text": "用户提问"}]},
                {"type": "agentMessage", "phase": "commentary", "text": "处理中"},
                {"type": "agentMessage", "phase": "final_answer", "text": "最终回答"},
            ]}]}
        return {"id": thread_id, "turns": []}

    def latest_result(self, account, thread_id):
        return {"status": self.thread["lastTurnStatus"], "text": self.result_text, "turnId": self.turn_id}

    def last_turn_status(self, account, thread_id):
        return self.thread["lastTurnStatus"]

    def archive(self, account, thread_id):
        self.calls.append(("archive", thread_id))
        self.thread["archived"] = True

    def unarchive(self, account, thread_id):
        self.calls.append(("unarchive", thread_id))
        self.thread["archived"] = False

    def delete(self, account, thread_id):
        self.calls.append(("delete", thread_id))


def make_config(tmp):
    return {
        "stateDir": tmp, "state": {"saveDebounceMs": 0},
        "wechat": {"baseUrl": "https://example.test", "botType": "3", "routeTag": None},
        "codex": {"bin": "codex", "workingDirectory": tmp, "timeoutMs": 1000,
                  "defaultAccount": "main", "accounts": [{"name": "main", "codexHome": tmp}]},
        "claude": {"bin": "claude", "defaultAccount": "main",
                   "accounts": [{"name": "main", "claudeConfigDir": tmp}]},
        "concurrency": {"maxWorkers": 1, "commandWorkers": 1, "perConversationSerial": True},
        "media": {"maxFileBytes": 1024, "maxConcurrentTransfers": 1, "generators": []},
        "allowedUsers": [], "adminUsers": ["user-1"], "textChunkLimit": 4000,
    }


class DesktopCatalogTests(unittest.TestCase):
    def test_project_uses_longest_matching_root(self):
        projects = [
            {"id": "a", "roots": ["/tmp/project"]},
            {"id": "b", "roots": ["/tmp/project/mobile"]},
        ]
        self.assertEqual(project_for_thread({"cwd": "/tmp/project/mobile/app"}, projects), "b")
        self.assertEqual(project_for_thread({"cwd": "/tmp/unrelated"}, projects), "_ungrouped")

    def test_reads_projects_and_all_thread_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner()
            catalog = DesktopCodexCatalog(runner)
            account = {"name": "main", "codexHome": tmp}
            self.assertEqual(catalog.projects(account)[0]["name"], "手机控制助手")
            threads = catalog.threads(account)
            self.assertEqual([item["id"] for item in threads], ["thread-1", "thread-2"])
            self.assertEqual([method for method, _ in runner.server.requests],
                             ["project/list", "thread/list", "thread/list"])
            created = catalog.create_project(account, "新项目", "/tmp/new", "unique-key")
            self.assertEqual(created, {"id": "p2", "name": "新项目", "roots": ["/tmp/new"]})
            self.assertEqual(runner.server.requests[-1], ("project/create", {
                "name": "新项目", "roots": [{"path": "/tmp/new"}], "idempotencyKey": "unique-key",
            }))
            self.assertEqual(catalog.latest_result(account, "thread-1"), {
                "status": "completed", "text": "最新完整结果", "turnId": "new",
            })

    def test_unmaterialized_thread_has_no_result_yet(self):
        catalog = DesktopCodexCatalog(EmptyThreadRunner())
        account = {"name": "main", "codexHome": "/tmp"}
        self.assertEqual(catalog.last_turn_status(account, "new-thread"), "unknown")
        self.assertEqual(catalog.latest_result(account, "new-thread"), {
            "status": "unknown", "text": "", "turnId": "",
        })


class DesktopCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = MultiWechatCodexService(make_config(self.tmp.name))
        self.fake = FakeDesktop()
        self.service.desktop = self.fake
        self.sent = []
        self.service._send_text = lambda account, user_id, text: self.sent.append(text)
        self.account = {"accountId": "acct-1"}
        self.key = self.service.state.conversation_key("acct-1", "user-1")

    def tearDown(self):
        self.service.codex.terminate_all()
        self.tmp.cleanup()

    def command(self, text):
        self.service._handle_message(self.account, "user-1", self.key, text)
        return self.sent[-1]

    def test_short_desktop_commands(self):
        self.assertIn("手机控制助手", self.command("/d-projects"))
        self.assertIn("设计随行助手", self.command("/d-sessions 1"))
        self.assertIn("最终回答", self.command("/d-session view 1"))
        self.assertIn("最新结果", self.command("/d-session use 1"))
        self.assertIn("桌面会话账号", self.command("/d-account"))
        self.assertIn("已归档", self.command("/d-session archive 1"))
        self.assertIn("归档会话", self.command("/d-sessions archived"))
        self.assertIn("已恢复", self.command("/d-session unarchive 1"))

    def test_new_project_creates_workspace_and_fresh_cli_session(self):
        target = Path(self.tmp.name) / "随行助手"
        result = self.command(f"/new-project {target}")
        self.assertIn("已切换到项目工作区：随行助手", result)
        self.assertTrue(target.is_dir())
        self.assertEqual(self.service.state.get_active_workspace(self.key), "随行助手")
        key = self.service.state.workspace_conversation_key(self.key, "随行助手")
        session = self.service._get_session(key)
        self.assertEqual(session["cwd"], str(target.resolve()))
        self.assertEqual(session["codexThreadId"], "")
        self.assertEqual(session["codexClient"], "")
        self.assertIn("已切换到项目工作区", self.command(f"/n-p {target}"))
        self.assertEqual(len(self.service.state.list_workspaces(self.key)), 1)

    def test_new_project_resolves_name_collision(self):
        self.command(f"/new-project {Path(self.tmp.name) / 'a' / 'demo'}")
        self.command(f"/new-project {Path(self.tmp.name) / 'b' / 'demo'}")
        self.assertEqual(self.service.state.get_active_workspace(self.key), "demo-2")
        self.assertEqual({item["name"] for item in self.service.state.list_workspaces(self.key)}, {"demo", "demo-2"})

    def test_new_desktop_project_aliases_prepare_fresh_sessions(self):
        first = Path(self.tmp.name) / "project-a"
        result = self.command(f"/d-p-n {first}")
        self.assertIn("已创建桌面 Codex 项目", result)
        self.assertIn("已准备新的桌面 Codex 会话", result)
        self.assertEqual(self.service.state.get_active_workspace(self.key), "default")
        self.assertEqual(self.service.state.list_workspaces(self.key), [])
        session = self.service._get_session(self.key)
        self.assertEqual(session["codexClient"], "desktop")
        self.assertEqual(session["codexThreadId"], "")
        self.assertEqual(session["desktopProjectId"], "project-2")
        self.assertEqual(self.service.codex._session_agent(self.key), "desktop")
        persisted = StateStore(self.tmp.name).get_session(self.key, self.tmp.name)
        self.assertEqual(persisted["codexClient"], "desktop")
        self.assertEqual(persisted["codexThreadId"], "")
        self.assertIn("project-a", self.command("/d-projects"))
        self.assertNotIn("微信工作区", self.sent[-1])
        self.fake.thread["cwd"] = str(first.resolve())
        self.fake.thread["projectId"] = "project-2"
        self.assertIn("设计随行助手", self.command("/d-sessions 2"))
        second = Path(self.tmp.name) / "project-b"
        self.assertIn("已准备新的桌面 Codex 会话", self.command(f"/d-n-p {second}"))
        self.assertEqual(self.service._get_session(self.key)["desktopProjectId"], "project-3")
        self.assertIn("已切换到已有桌面项目", self.command(f"/d-new-project {first}"))
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "project/create"]), 2)

    def test_new_desktop_session_uses_current_workspace(self):
        self.service.state.update_session(self.key, cwd="/tmp/project")
        self.assertIn("已准备新的桌面 Codex 会话", self.command("/d-session new"))
        session = self.service._get_session(self.key)
        self.assertEqual(session["codexClient"], "desktop")
        self.assertEqual(session["codexThreadId"], "")
        self.assertEqual(session["desktopProjectId"], "project-1")

    def test_switch_native_project_keeps_project_identity_for_new_thread(self):
        target = Path(self.tmp.name) / "native-project"
        self.command(f"/d-p-n {target}")
        self.command("/d-projects")
        self.assertIn("已切换桌面项目：手机控制助手", self.command("/d-project use 1"))
        self.assertEqual(self.service.state.list_workspaces(self.key), [])
        session = self.service._get_session(self.key)
        self.assertEqual(session["desktopProjectId"], "project-1")
        self.assertEqual(session["cwd"], "/tmp/project")
        pending_key = self.service._conversation_key_for_text(self.key, "第一条任务")
        self.assertEqual(self.service._get_session(pending_key)["desktopProjectId"], "project-1")

    def test_pending_desktop_thread_routes_and_promotes_after_completion(self):
        self.command("/d-session new")
        pending_key = self.service._conversation_key_for_text(self.key, "第一条任务")
        self.assertIn(":pending-", pending_key)
        self.assertTrue(self.service._get_session(pending_key)["desktopPendingThread"])
        self.service.state.update_session(pending_key, codexThreadId="new-thread-123")
        self.assertEqual(self.service._conversation_key_for_text(self.key, "补充要求"), pending_key)
        self.service._promote_pending_desktop_thread(pending_key)
        resumed_key = self.service._conversation_key_for_text(self.key, "后续任务")
        self.assertIn("new-thread-123", resumed_key)
        self.assertEqual(self.service._get_session(self.key)["codexThreadId"], "new-thread-123")

    def test_first_pending_desktop_turn_delivers_result_and_sets_thread(self):
        self.command("/d-session new")
        pending_key = self.service._conversation_key_for_text(self.key, "第一条任务")
        self.service._start_typing_loop = lambda account, user_id: lambda: None

        def run(key, text):
            self.service.state.update_session(key, codexThreadId="new-thread-123")
            return "最终回答"

        self.service.codex.run = run
        self.service._run_codex_and_reply(self.account, "user-1", pending_key, "第一条任务")
        self.assertEqual(self.service._get_session(self.key)["codexThreadId"], "new-thread-123")
        self.assertIn("最终回答", self.sent[-1])

    def test_first_pending_desktop_turn_stays_silent_after_switch(self):
        self.command("/d-session new")
        pending_key = self.service._conversation_key_for_text(self.key, "第一条任务")
        self.command("/d-sessions all")
        self.command("/d-session use 1")
        self.service._start_typing_loop = lambda account, user_id: lambda: None

        def run(key, text):
            self.service.state.update_session(key, codexThreadId="new-thread-123")
            return "后台结果"

        self.service.codex.run = run
        sent_before = len(self.sent)
        self.service._run_codex_and_reply(self.account, "user-1", pending_key, "第一条任务")
        self.assertEqual(len(self.sent), sent_before)
        self.assertEqual(self.service._get_session(self.key)["codexThreadId"], self.fake.thread["id"])

    def test_cli_codex_archive_restore_and_confirmed_delete(self):
        db = sqlite3.connect(str(Path(self.tmp.name) / "state_5.sqlite"))
        db.execute("create table threads (id text, title text, cwd text, source text, created_at integer, updated_at integer, archived integer)")
        db.execute("insert into threads values (?, ?, ?, ?, ?, ?, ?)",
                   (self.fake.thread["id"], "CLI 任务", self.tmp.name, "exec", 1, 2, 0))
        db.commit()
        self.assertIn("CLI 任务", self.command("/sessions codex"))
        self.assertIn("已归档", self.command("/session archive 1"))
        self.assertIn(("archive", self.fake.thread["id"]), self.fake.calls)
        db.execute("update threads set archived = 1")
        db.commit()
        self.assertIn("CLI 任务", self.command("/sessions archived codex"))
        self.assertIn("已恢复", self.command("/session unarchive 1"))
        db.execute("update threads set archived = 0")
        db.commit()
        self.command("/sessions codex")
        self.assertIn("即将永久删除", self.command("/session delete 1"))
        self.assertNotIn(("delete", self.fake.thread["id"]), self.fake.calls)
        self.assertIn("已永久删除", self.command("/session delete 1 confirm"))
        self.assertIn(("delete", self.fake.thread["id"]), self.fake.calls)
        db.close()

    def test_cli_claude_archive_restore_and_confirmed_delete(self):
        project = Path(self.tmp.name) / "projects" / "-tmp"
        project.mkdir(parents=True)
        log = project / "claude-123.jsonl"
        log.write_text('{"type":"user","message":{"content":"Claude 任务"}}\n', encoding="utf-8")
        self.assertIn("Claude 任务", self.command("/sessions claude"))
        self.assertIn("已归档", self.command("/session archive 1"))
        self.assertEqual(self.service.state.archived_claude_ids("main"), {"claude-123"})
        self.assertIn("Claude 任务", self.command("/sessions archived claude"))
        self.assertIn("已恢复", self.command("/session unarchive 1"))
        self.command("/sessions claude")
        self.assertIn("即将永久删除", self.command("/session delete 1"))
        self.assertTrue(log.exists())
        self.assertIn("已永久删除", self.command("/session delete 1 confirm"))
        self.assertFalse(log.exists())

    def test_cli_delete_requires_admin(self):
        project = Path(self.tmp.name) / "projects" / "-tmp"
        project.mkdir(parents=True)
        log = project / "claude-123.jsonl"
        log.write_text('{"type":"user","message":{"content":"任务"}}\n', encoding="utf-8")
        self.command("/sessions claude")
        self.service.config["adminUsers"] = []
        with self.assertRaisesRegex(ValueError, "仅限 adminUsers"):
            self.command("/session delete 1")
        self.assertTrue(log.exists())

    def test_project_chat_number_and_desktop_continuation_route(self):
        self.assertIn("手机控制助手", self.command("/desktop projects"))
        self.fake.thread["lastTurnStatus"] = "interrupted"
        self.fake.last_turn_status = lambda account, thread_id: "completed"
        self.assertIn("设计随行助手", self.command("/desktop chats 1"))
        self.assertIn("最近回合已完成", self.sent[-1])
        self.assertIn("最近回合已完成", self.command("/desktop status 1"))
        self.fake.thread["lastTurnStatus"] = "completed"
        viewed = self.command("/desktop view 1")
        self.assertIn("最终回答", viewed)
        self.assertNotIn("处理中", viewed)
        self.assertNotIn("旧回答", viewed)
        self.assertIn("最新结果：", self.command("/desktop use 1"))
        session = self.service.state.get_session(self.key, self.tmp.name)
        self.assertEqual(session["codexThreadId"], self.fake.thread["id"])
        self.assertEqual(self.service.codex._session_agent(self.key), "desktop")

    def test_running_thread_can_be_left_and_result_is_shown_on_return(self):
        self.command("/desktop chats all")
        self.command("/desktop use 1")
        first_run_key = self.service._conversation_key_for_text(self.key, "任务 A")
        self.assertNotEqual(first_run_key, self.key)
        context = AppTurnState(first_run_key, self.fake.thread["id"])
        context.start_turn("turn-a")
        self.fake.runner.contexts[first_run_key] = context

        other = dict(self.fake.thread, id="thread-other", title="任务 B")
        self.fake.threads = lambda account, archived=False: [dict(self.fake.thread), other] if not archived else []
        self.command("/desktop chats all")
        self.assertIn("已切换：任务 B", self.command("/desktop use 2"))
        second_run_key = self.service._conversation_key_for_text(self.key, "任务 B")
        self.assertNotEqual(first_run_key, second_run_key)

        self.service._start_typing_loop = lambda account, user_id: lambda: None
        self.service.codex.run = lambda conversation_key, message: "A 的完整结果"
        sent_before = len(self.sent)
        self.service._run_codex_and_reply(self.account, "user-1", first_run_key, "任务 A")
        self.assertEqual(len(self.sent), sent_before)

        self.command("/desktop chats all")
        self.assertIn("正在执行中", self.command("/desktop use 1"))
        context.finish("completed")
        self.command("/desktop use 2")
        self.assertIn("最新结果：\n最终回答", self.command("/desktop use 1"))

    def test_current_thread_sends_only_full_latest_final_result(self):
        self.command("/desktop chats all")
        self.command("/desktop use 1")
        self.fake.result_text = "最新完整结果" + "长" * 900
        self.fake.turn_id = "turn-new"
        run_key = self.service._conversation_key_for_text(self.key, "任务")
        self.service._start_typing_loop = lambda account, user_id: lambda: None
        self.service.codex.run = lambda conversation_key, message: "过程消息\n最终回答"
        self.service._run_codex_and_reply(self.account, "user-1", run_key, "任务")
        self.assertIn(self.fake.result_text, self.sent[-1])
        self.assertNotIn("过程消息", self.sent[-1])

    def test_switching_workspaces_returns_latest_result(self):
        self.command("/desktop chats all")
        self.command("/desktop use 1")
        run_key = self.service._conversation_key_for_text(self.key, "任务 A")
        self.service.state.upsert_workspace(self.key, "other", self.tmp.name)
        self.command("/ws use other")
        self.fake.result_text = "A 后台完成的结果"
        self.fake.turn_id = "turn-after-switch"
        self.service._start_typing_loop = lambda account, user_id: lambda: None
        self.service.codex.run = lambda conversation_key, message: self.fake.result_text
        sent_before = len(self.sent)
        self.service._run_codex_and_reply(self.account, "user-1", run_key, "任务 A")
        self.assertEqual(len(self.sent), sent_before)
        self.assertIn(self.fake.result_text, self.command("/ws use default"))

    def test_delete_requires_second_matching_confirmation(self):
        self.command("/desktop chats all")
        self.assertIn("永久删除", self.command("/desktop delete 1"))
        self.assertFalse(any(call[0] == "delete" for call in self.fake.calls))
        self.assertIn("已永久删除", self.command("/desktop delete 1 confirm"))
        self.assertIn(("delete", self.fake.thread["id"]), self.fake.calls)

    def test_archive_clears_selected_thread_and_unarchive_restores(self):
        self.command("/desktop chats all")
        self.command("/desktop use 1")
        self.assertIn("已归档", self.command("/desktop archive 1"))
        session = self.service.state.get_session(self.key, self.tmp.name)
        self.assertEqual(session["codexThreadId"], "")
        self.assertEqual(session["codexClient"], "")
        self.command("/desktop archived all")
        self.assertIn("已恢复", self.command("/desktop unarchive 1"))

    def test_non_admin_cannot_delete(self):
        self.service.config["adminUsers"] = []
        self.command("/desktop chats all")
        with self.assertRaisesRegex(ValueError, "adminUsers"):
            self.command("/desktop delete 1")


if __name__ == "__main__":
    unittest.main()
