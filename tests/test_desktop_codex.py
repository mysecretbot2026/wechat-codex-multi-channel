import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_codex_multi.codex_app_server import AppTurnState
from wechat_codex_multi.desktop_codex import DesktopCodexCatalog, project_for_thread
from wechat_codex_multi.service import MultiWechatCodexService
from wechat_codex_multi.state import StateStore


class FakeServer:
    def __init__(self):
        self.requests = []
        self.turns = [
            {"id": "old", "status": "completed", "items": [
                {"type": "agentMessage", "phase": "final_answer", "text": "旧结果"},
            ]},
            {"id": "new", "status": "completed", "items": [
                {"type": "agentMessage", "phase": "commentary", "text": "过程"},
                {"type": "agentMessage", "phase": "final_answer", "text": "最新完整结果"},
            ]},
        ]

    def request(self, method, params=None, timeout_s=30):
        self.requests.append((method, params or {}))
        if method == "project/list":
            return {"data": [{"id": "p1", "name": "手机控制助手",
                              "roots": [{"path": "/tmp/project"}]}], "nextCursor": None}
        if method == "project/create":
            return {"project": {"id": "p2", "name": params["name"], "roots": params["roots"]}}
        if method == "thread/read":
            return {"thread": {"turns": self.turns}}
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
        self.previous_result_text = "旧回答"
        self.models = [{"model": "gpt-test", "reasoningEffort": effort, "defaultReasoningEffort": "high"}
                       for effort in ("low", "high")]
        self.thread = {
            "id": "thread-1234567890", "title": "设计随行助手", "cwd": "/tmp/project",
            "projectId": "project-1",
            "updatedAt": 100, "lastTurnStatus": "completed", "runtimeStatus": "notLoaded",
            "archived": False,
            "model": "gpt-native", "reasoningEffort": "xhigh",
        }

        self.project_list = [{"id": "project-1", "name": "手机控制助手", "roots": ["/tmp/project"]}]

    def projects(self, account):
        return [dict(project) for project in self.project_list]

    def model_options(self, account):
        self.calls.append(("model/list", account["name"]))
        return list(self.models)

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
        return {"id": thread_id, "turns": [], "model": self.thread.get("model"),
                "reasoningEffort": self.thread.get("reasoningEffort")}

    def latest_result(self, account, thread_id):
        completed = self.thread["lastTurnStatus"] == "completed"
        return {
            "status": self.thread["lastTurnStatus"],
            "text": self.result_text if completed else "",
            "turnId": self.turn_id,
            "lastAnswerText": self.result_text if completed else self.previous_result_text,
            "lastAnswerTurnId": self.turn_id if completed else "turn-old",
        }

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
    def test_native_model_menu_reads_all_pages_and_filters_hidden(self):
        runner = FakeRunner()
        first = {"data": [{"model": "gpt-test", "displayName": "Test", "defaultReasoningEffort": "high",
                           "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}]},
                          {"model": "hidden", "hidden": True}], "nextCursor": "next"}
        second = {"data": [{"model": "gpt-other", "defaultReasoningEffort": "medium"}], "nextCursor": None}
        account = {"name": "backup", "codexHome": "/tmp/backup"}
        with patch.object(runner, "request_for_account", side_effect=[first, second]) as request:
            options = DesktopCodexCatalog(runner).model_options(account)
        self.assertEqual([(o["model"], o["reasoningEffort"]) for o in options],
                         [("gpt-test", "low"), ("gpt-test", "high"), ("gpt-other", "medium")])
        self.assertEqual(options[0]["defaultReasoningEffort"], "high")
        self.assertTrue(all(call.args[0] == account and call.args[1] == "model/list" for call in request.call_args_list))
        self.assertEqual(request.call_args_list[1].args[2]["cursor"], "next")

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
                "lastAnswerText": "最新完整结果", "lastAnswerTurnId": "new",
            })

    def test_running_turn_keeps_last_completed_answer_for_switch_recap(self):
        runner = FakeRunner()
        runner.server.turns[-1] = {"id": "running", "status": "inProgress", "items": [
            {"type": "userMessage", "content": [{"type": "text", "text": "新任务"}]},
        ]}
        catalog = DesktopCodexCatalog(runner)
        self.assertEqual(catalog.latest_result({"name": "main", "codexHome": "/tmp"}, "thread-1"), {
            "status": "inProgress", "text": "", "turnId": "running",
            "lastAnswerText": "旧结果", "lastAnswerTurnId": "old",
        })

    def test_unmaterialized_thread_has_no_result_yet(self):
        catalog = DesktopCodexCatalog(EmptyThreadRunner())
        account = {"name": "main", "codexHome": "/tmp"}
        self.assertEqual(catalog.last_turn_status(account, "new-thread"), "unknown")
        self.assertEqual(catalog.latest_result(account, "new-thread"), {
            "status": "unknown", "text": "", "turnId": "",
            "lastAnswerText": "", "lastAnswerTurnId": "",
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

    def select_desktop_thread(self):
        self.command("/d-sessions all")
        self.command("/d-session 1")
        return self.service._desktop_execution_key(self.key)

    def test_status_reads_native_model_and_full_thread_id_without_resuming(self):
        run_key = self.select_desktop_thread()
        self.service.config["codex"].update(model="wrong-global", reasoningEffort="low")
        self.service.state.update_session(run_key, codexModel="stale-cache")
        status = self.command("/status")
        self.assertIn("codexModel: gpt-native", status)
        self.assertIn("reasoning: xhigh", status)
        self.assertIn("codexThreadId: " + self.fake.thread["id"], status)
        self.assertIn("默认沿用", status)
        self.fake.thread.update(model="desktop-changed", reasoningEffort="medium")
        self.assertIn("codexModel: desktop-changed", self.command("/status"))
        self.assertIn("codexModel: desktop-changed", self.command("/d-session status 1"))
        self.assertNotIn("thread/resume", [call[0] for call in self.fake.calls])

    def test_model_switch_preserves_thread_and_survives_route_refresh(self):
        run_key = self.select_desktop_thread()
        self.assertEqual(self.service._conversation_key_for_text(self.key, "/model 2"), run_key)
        self.assertEqual(self.service._conversation_key_for_text(self.key, "/models"), run_key)
        self.assertIn("当前: gpt-native:xhigh", self.command("/model"))
        self.assertIn("下一轮将使用 gpt-test:high", self.command("/model 2"))
        for text in ("/status", "第二轮任务"):
            self.assertEqual(self.service._conversation_key_for_text(self.key, text), run_key)
        selected = self.service._get_session(run_key)
        self.assertEqual(selected["codexThreadId"], self.fake.thread["id"])
        self.assertEqual(selected["desktopModelOverride"], {"model": "gpt-test", "reasoningEffort": "high"})
        self.assertIn("codexModel: gpt-test", self.command("/status"))
        self.assertIn("下一轮生效", self.sent[-1])
        self.assertIn("恢复自动沿用", self.command("/model auto"))
        self.assertEqual(self.service._get_session(run_key)["desktopModelOverride"], {})
        self.assertIn("codexModel: gpt-native", self.command("/status"))

    def test_model_number_uses_last_displayed_menu_and_invalid_choice_changes_nothing(self):
        run_key = self.select_desktop_thread()
        self.command("/models")
        self.fake.models.reverse()
        self.command("/model 1")
        self.assertEqual(self.service._get_session(run_key)["desktopModelOverride"]["reasoningEffort"], "low")
        self.assertIn("未知模型选项", self.command("/model gpt-test:invalid"))
        self.assertEqual(self.service._get_session(run_key)["desktopModelOverride"]["reasoningEffort"], "low")

    def test_model_override_is_isolated_between_selected_threads(self):
        first_id = self.fake.thread["id"]
        first_key = self.select_desktop_thread()
        self.command("/model gpt-test:high")
        self.fake.thread["id"] = "other-thread"
        second_key = self.select_desktop_thread()
        self.assertFalse(self.service._get_session(second_key).get("desktopModelOverride"))
        self.command("/model gpt-test:low")
        self.fake.thread["id"] = first_id
        self.assertEqual(self.select_desktop_thread(), first_key)
        self.assertEqual(self.service._get_session(first_key)["desktopModelOverride"]["reasoningEffort"], "high")

    def test_pending_new_thread_preserves_model_and_promotes_it(self):
        self.command("/d-projects")
        self.command("/d-project use 1")
        pending_key = self.service._desktop_execution_key(self.key)
        self.command("/model gpt-test:high")
        expected = {"model": "gpt-test", "reasoningEffort": "high"}
        self.assertEqual(self.service._get_session(pending_key)["desktopModelOverride"], expected)
        self.service.state.update_session(pending_key, codexThreadId="new-native-thread", codexModel="gpt-test",
                                          codexReasoningEffort="high")
        self.service._promote_pending_desktop_thread(pending_key)
        new_key = self.service._desktop_execution_key(self.key)
        self.assertEqual(self.service._get_session(new_key)["desktopModelOverride"], expected)
        self.assertEqual(self.service._get_session(new_key)["codexModel"], "gpt-test")

    def test_model_read_failure_is_explicit_and_auto_works_without_model_list(self):
        run_key = self.select_desktop_thread()
        self.command("/status")
        self.command("/model gpt-test:high")
        with patch.object(self.fake, "read", side_effect=RuntimeError("read failed")), \
                patch.object(self.fake, "model_options", side_effect=RuntimeError("list failed")):
            status = self.command("/status")
            self.assertIn("codexModel: gpt-test", status)
            self.assertIn("读取会话模型失败", status)
            self.assertIn("恢复自动沿用", self.command("/model auto"))
        self.assertFalse(self.service._get_session(run_key)["desktopModelOverride"])

    def test_model_menu_uses_selected_desktop_account(self):
        self.service.config["codex"]["accounts"].append({"name": "backup", "codexHome": self.tmp.name + "/backup"})
        self.command("/d-account backup")
        self.select_desktop_thread()
        self.command("/models")
        self.assertIn(("model/list", "backup"), self.fake.calls)

    def test_short_desktop_commands(self):
        self.assertIn("手机控制助手", self.command("/d-projects"))
        self.assertIn("设计随行助手", self.command("/d-sessions 1"))
        self.assertIn("最终回答", self.command("/d-session view 1"))
        self.assertIn("最新结果", self.command("/d-session use 1"))
        self.assertIn("桌面会话账号", self.command("/d-account"))
        self.assertIn("已归档", self.command("/d-session archive 1"))
        self.assertIn("归档会话", self.command("/d-sessions archived"))
        self.assertIn("已恢复", self.command("/d-session unarchive 1"))

    def test_desktop_pagination_and_number_shortcut(self):
        threads = [dict(self.fake.thread, id=f"thread-{index:03d}", title=f"会话{index}",
                        updatedAt=100 - index) for index in range(1, 46)]
        self.fake.threads = lambda account, archived=False: [] if archived else [dict(item) for item in threads]
        first_page = self.command("/d-sessions all")
        self.assertIn("第 1/3 页", first_page)
        self.assertIn("下一页：/d-sessions all 2", first_page)
        self.assertNotIn("21. 会话21", first_page)
        second_page = self.command("/d-sessions page 2")
        self.assertIn("21. 会话21", second_page)
        self.assertIn("上一页：/d-sessions all 1", second_page)
        self.assertIn("下一页：/d-sessions all 3", second_page)
        self.assertIn("最新结果", self.command("/d-session 21"))
        self.assertEqual(self.service._get_session(self.key)["codexThreadId"], "thread-021")
        self.assertIn("第 2/3 页", self.command("/d-sessions 1 2"))

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

    def cache_delete_batch(self, desktop):
        self.fake.calls.clear()
        self.service.session_selection_cache.clear()
        self.service.desktop_thread_cache.clear()
        self.service.session_delete_pending.clear()
        self.service.desktop_delete_pending.clear()
        items = [dict(self.fake.thread, id=f"thread-{index}", sessionId=f"thread-{index}",
                      title=f"会话{index}", agent="codex", account="main") for index in range(1, 6)]
        cache = self.service.desktop_thread_cache if desktop else self.service.session_selection_cache
        cache[self.key] = items
        pending = self.service.desktop_delete_pending if desktop else self.service.session_delete_pending
        return ("/d-session delete" if desktop else "/session delete"), cache, pending

    def test_batch_delete_previews_all_targets_and_clears_only_deleted_refs(self):
        for desktop in (False, True):
            with self.subTest(desktop=desktop):
                command, cache, pending = self.cache_delete_batch(desktop)
                for index in range(1, 6):
                    self.service.state.update_session(f"{self.key}:ref-{index}",
                        codexThreadId=f"thread-{index}", codexAccount="main")
                preview = self.command(f"{command} 3 4 5")
                self.assertIn("即将永久删除 3 条会话", preview)
                self.assertIn(f"{command} 3 4 5 confirm", preview)
                for index in (3, 4, 5):
                    self.assertIn(f"thread-{index}", preview)
                self.assertFalse(any(call[0] == "delete" for call in self.fake.calls))
                result = self.command(f"{command} 3 4 5 confirm")
                self.assertIn("已永久删除 3 条会话", result)
                self.assertEqual([call[1] for call in self.fake.calls if call[0] == "delete"],
                                 ["thread-3", "thread-4", "thread-5"])
                self.assertNotIn(self.key, cache)
                self.assertNotIn(self.key, pending)
                for index in range(1, 6):
                    expected = "" if index in (3, 4, 5) else f"thread-{index}"
                    self.assertEqual(self.service._get_session(f"{self.key}:ref-{index}")["codexThreadId"], expected)

    def test_batch_delete_deduplicates_ids_and_accepts_reordered_confirmation(self):
        for desktop in (False, True):
            with self.subTest(desktop=desktop):
                command, _, _ = self.cache_delete_batch(desktop)
                self.assertIn("即将永久删除 2 条会话", self.command(f"{command} 3 thread-3 4 3"))
                self.command(f"{command} 4 3 confirm")
                self.assertEqual([call[1] for call in self.fake.calls if call[0] == "delete"],
                                 ["thread-4", "thread-3"])

    def test_batch_delete_requires_preview_of_the_entire_same_set(self):
        for desktop in (False, True):
            for preview in (None, "1", "1 2 3", "2 3"):
                with self.subTest(desktop=desktop, preview=preview):
                    command, _, _ = self.cache_delete_batch(desktop)
                    if preview:
                        self.command(f"{command} {preview}")
                    with self.assertRaisesRegex(ValueError, "删除确认已失效"):
                        self.command(f"{command} 1 2 confirm")
                    self.assertFalse(any(call[0] == "delete" for call in self.fake.calls))

    def test_batch_delete_rejects_expired_confirmation(self):
        for desktop in (False, True):
            with self.subTest(desktop=desktop):
                command, _, pending = self.cache_delete_batch(desktop)
                self.command(f"{command} 1 2")
                pending[self.key]["expires"] = 0
                with self.assertRaisesRegex(ValueError, "删除确认已失效"):
                    self.command(f"{command} 1 2 confirm")
                self.assertFalse(any(call[0] == "delete" for call in self.fake.calls))

    def test_batch_delete_does_not_follow_changed_list_numbers(self):
        for desktop in (False, True):
            with self.subTest(desktop=desktop):
                command, cache, _ = self.cache_delete_batch(desktop)
                self.command(f"{command} 1 2")
                cache[self.key].reverse()
                with self.assertRaisesRegex(ValueError, "所选会话发生变化"):
                    self.command(f"{command} 1 2 confirm")
                self.assertFalse(any(call[0] == "delete" for call in self.fake.calls))

    def test_batch_delete_confirmation_is_bound_to_the_account(self):
        self.service.config["codex"]["accounts"].append({"name": "backup", "codexHome": self.tmp.name + "/backup"})
        for desktop in (False, True):
            with self.subTest(desktop=desktop):
                command, cache, _ = self.cache_delete_batch(desktop)
                self.command(f"{command} 1 2")
                cache[self.key][1]["account"] = "backup"
                with self.assertRaisesRegex(ValueError, "所选会话发生变化"):
                    self.command(f"{command} 1 2 confirm")
                self.assertFalse(any(call[0] == "delete" for call in self.fake.calls))

    def test_batch_delete_rejects_invalid_targets_and_malformed_confirmation(self):
        for desktop in (False, True):
            for args in ("", "confirm", "1 99", "1 missing-id", "1 thread", "1 confirm 2", "1 2 confirm extra"):
                with self.subTest(desktop=desktop, args=args):
                    command, _, pending = self.cache_delete_batch(desktop)
                    with self.assertRaises(ValueError):
                        self.command(f"{command} {args}".strip())
                    self.assertNotIn(self.key, pending)
                    self.assertFalse(any(call[0] == "delete" for call in self.fake.calls))

    def test_batch_delete_checks_running_targets_at_preview_and_confirmation(self):
        for desktop in (False, True):
            for confirming in (False, True):
                with self.subTest(desktop=desktop, confirming=confirming):
                    command, _, _ = self.cache_delete_batch(desktop)
                    if confirming:
                        self.command(f"{command} 3 4 5")
                    status = lambda account, thread_id: "inProgress" if thread_id == "thread-4" else "completed"
                    with patch.object(self.fake, "last_turn_status", side_effect=status):
                        with self.assertRaisesRegex(ValueError, "正在运行"):
                            self.command(f"{command} 3 4 5" + (" confirm" if confirming else ""))
                    self.assertFalse(any(call[0] == "delete" for call in self.fake.calls))

    def test_batch_delete_rejects_running_cli_refs_even_for_desktop_commands(self):
        for desktop in (False, True):
            with self.subTest(desktop=desktop):
                command, _, _ = self.cache_delete_batch(desktop)
                self.service.state.update_session(f"{self.key}:running", codexThreadId="thread-4", codexAccount="main")
                with patch.object(self.service.codex, "is_running", side_effect=lambda key: key.endswith(":running")):
                    with self.assertRaisesRegex(ValueError, "正在运行"):
                        self.command(f"{command} 3 4 5")
                self.assertFalse(any(call[0] == "delete" for call in self.fake.calls))

    def test_batch_delete_stops_and_reports_partial_failure(self):
        for desktop in (False, True):
            with self.subTest(desktop=desktop):
                command, cache, pending = self.cache_delete_batch(desktop)
                self.command(f"{command} 3 4 5")
                def delete(account, thread_id):
                    if thread_id == "thread-4":
                        raise RuntimeError("delete failed")
                    self.fake.calls.append(("delete", thread_id))
                with patch.object(self.fake, "delete", side_effect=delete) as request:
                    result = self.command(f"{command} 3 4 5 confirm")
                self.assertIn("已完成 1/3 条", result)
                self.assertIn("已永久删除：会话3（thread-3）", result)
                self.assertIn("处理失败：会话4（thread-4）：delete failed", result)
                self.assertIn("后续 1 条未执行", result)
                self.assertEqual([call.args[1] for call in request.call_args_list], ["thread-3", "thread-4"])
                self.assertNotIn(self.key, cache)
                self.assertNotIn(self.key, pending)

    def test_batch_delete_requires_admin_for_preview_and_confirmation(self):
        for desktop in (False, True):
            with self.subTest(desktop=desktop):
                self.service.config["adminUsers"] = ["user-1"]
                command, _, _ = self.cache_delete_batch(desktop)
                self.command(f"{command} 1 2")
                self.service.config["adminUsers"] = []
                for suffix in ("", " confirm"):
                    with self.assertRaisesRegex(ValueError, "仅限 adminUsers"):
                        self.command(f"{command} 1 2{suffix}")
                self.assertFalse(any(call[0] == "delete" for call in self.fake.calls))

    def test_cli_batch_delete_mixes_codex_and_claude_and_removes_metadata(self):
        command, cache, _ = self.cache_delete_batch(False)
        files = []
        for index in (4, 5):
            session_id = f"claude-{index}"
            cache[self.key][index - 1].update(agent="claude", sessionId=session_id)
            for path in (Path(self.tmp.name) / "projects" / "example" / f"{session_id}.jsonl",
                         Path(self.tmp.name) / "usage-data" / "session-meta" / f"{session_id}.json"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
                files.append(path)
            self.service.state.set_claude_archived("main", session_id, True)
            self.service.state.update_session(f"{self.key}:claude-{index}", claudeSessionId=session_id, claudeAccount="main")
        preview = self.command(f"{command} 3 4 5")
        self.assertIn("codex:main", preview)
        self.assertIn("claude:main", preview)
        self.assertTrue(all(path.exists() for path in files))
        self.assertIn("已永久删除 3 条会话", self.command(f"{command} 3 4 5 confirm"))
        self.assertEqual([call[1] for call in self.fake.calls if call[0] == "delete"], ["thread-3"])
        self.assertFalse(any(path.exists() for path in files))
        self.assertEqual(self.service.state.archived_claude_ids("main"), set())
        for index in (4, 5):
            self.assertEqual(self.service._get_session(f"{self.key}:claude-{index}")["claudeSessionId"], "")

    def test_cli_batch_delete_preflights_missing_claude_files(self):
        command, cache, _ = self.cache_delete_batch(False)
        cache[self.key][3].update(agent="claude", sessionId="missing-claude")
        with self.assertRaisesRegex(ValueError, "没有找到可删除"):
            self.command(f"{command} 3 4 5")
        self.assertFalse(any(call[0] == "delete" for call in self.fake.calls))

    def test_cli_batch_delete_checks_all_files_again_before_confirmation(self):
        command, cache, _ = self.cache_delete_batch(False)
        cache[self.key][3].update(agent="claude", sessionId="claude-4")
        path = Path(self.tmp.name) / "projects" / "example" / "claude-4.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("{}\n", encoding="utf-8")
        self.command(f"{command} 3 4 5")
        path.unlink()
        with self.assertRaisesRegex(ValueError, "没有找到可删除"):
            self.command(f"{command} 3 4 5 confirm")
        self.assertFalse(any(call[0] == "delete" for call in self.fake.calls))

    def test_cli_batch_delete_preflights_claude_files_outside_account(self):
        command, cache, _ = self.cache_delete_batch(False)
        cache[self.key][3].update(agent="claude", sessionId="claude-link")
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "session.jsonl"
            target.write_text("{}\n", encoding="utf-8")
            link = Path(self.tmp.name) / "projects" / "example" / "claude-link.jsonl"
            link.parent.mkdir(parents=True)
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "路径异常"):
                self.command(f"{command} 3 4 5")
            self.assertTrue(target.exists())
        self.assertFalse(any(call[0] == "delete" for call in self.fake.calls))

    def test_legacy_desktop_batch_delete_and_single_target_still_work(self):
        self.cache_delete_batch(True)
        self.command("/desktop delete 3 4 5")
        self.assertIn("已永久删除 3 条会话", self.command("/desktop delete 3 4 5 confirm"))
        for desktop in (False, True):
            with self.subTest(desktop=desktop):
                command, _, _ = self.cache_delete_batch(desktop)
                self.command(f"{command} 3")
                self.assertIn("已永久删除：会话3", self.command(f"{command} 3 confirm"))
                self.assertEqual([call[1] for call in self.fake.calls if call[0] == "delete"], ["thread-3"])

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
        running_recap = self.command("/desktop use 1")
        self.assertIn("正在执行中", running_recap)
        self.assertIn("上一条完整回答：\n最终回答", running_recap)
        context.finish("completed")
        self.command("/desktop use 2")
        self.assertIn("最新结果：\n最终回答", self.command("/desktop use 1"))

    def test_switch_resends_latest_text_without_replaying_media_action(self):
        self.fake.result_text = "本轮结论\n[[send_file:/tmp/summary.pdf]]"
        self.service._api_for_account = lambda account: self.fail("切换会话不应重新发送媒体")
        self.command("/d-sessions all")
        first = self.command("/d-session use 1")
        second = self.command("/d-session use 1")
        self.assertEqual(first, second)
        self.assertIn("最新结果：\n本轮结论", second)
        self.assertNotIn("send_file", second)

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
