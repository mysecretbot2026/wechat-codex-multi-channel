import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_codex_multi.service import MultiWechatCodexService
from wechat_codex_multi.wechat import ITEM_IMAGE, ITEM_TEXT, MESSAGE_TYPE_USER


class FakeSteerRunner:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def steer(self, conversation_key, text):
        self.calls.append((conversation_key, text))
        return self.ok

    def cancel(self, conversation_key):
        return False

    def is_running(self, conversation_key):
        return True

    def terminate_all(self):
        self.calls.append(("terminate_all", ""))


class CapturingExecutor:
    def __init__(self):
        self.submissions = []

    def submit(self, fn, *args, **kwargs):
        self.submissions.append((fn, args, kwargs))
        return None

    def shutdown(self, **kwargs):
        return None


def make_test_config(state_dir):
    return {
        "stateDir": state_dir,
        "state": {"saveDebounceMs": 0},
        "wechat": {"baseUrl": "https://example.test", "routeTag": None},
        "codex": {
            "bin": "codex",
            "workingDirectory": state_dir,
            "timeoutMs": 1000,
            "bypassApprovalsAndSandbox": True,
            "defaultAccount": "main",
            "accounts": [{"name": "main", "codexHome": ""}],
        },
        "claude": {
            "bin": "claude",
            "model": "sonnet",
            "effort": "",
            "timeoutMs": 1000,
            "permissionMode": "bypassPermissions",
            "defaultAccount": "main",
            "accounts": [{"name": "main", "claudeConfigDir": ""}],
            "modelOptions": [],
        },
        "concurrency": {"maxWorkers": 1, "commandWorkers": 1, "perConversationSerial": True},
        "media": {"maxFileBytes": 1024, "maxConcurrentTransfers": 1, "generators": []},
        "allowedUsers": [],
        "adminUsers": [],
        "textChunkLimit": 4000,
    }


class ServicePerformanceTests(unittest.TestCase):
    def test_submit_message_does_not_download_inbound_media_on_monitor_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MultiWechatCodexService(make_test_config(tmp))
            service.executor = CapturingExecutor()
            service.command_executor = CapturingExecutor()
            account = {"accountId": "acct-1"}
            msg = {
                "message_type": MESSAGE_TYPE_USER,
                "from_user_id": "user-1",
                "context_token": "token-1",
                "item_list": [
                    {
                        "type": ITEM_IMAGE,
                        "image_item": {
                            "media": {
                                "encrypt_query_param": "param",
                                "aes_key": "YWJjZGVmZ2hpamtsbW5vcA==",
                            }
                        },
                    }
                ],
            }

            with patch("wechat_codex_multi.media.download_inbound_media") as download:
                service._submit_message(account, msg)

            download.assert_not_called()
            self.assertEqual(len(service.executor.submissions), 1)
            self.assertEqual(len(service.command_executor.submissions), 0)

    def test_commands_bypass_codex_worker_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MultiWechatCodexService(make_test_config(tmp))
            service.executor = CapturingExecutor()
            service.command_executor = CapturingExecutor()
            account = {"accountId": "acct-1"}
            msg = {
                "message_type": MESSAGE_TYPE_USER,
                "from_user_id": "user-1",
                "context_token": "token-1",
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "/status"}}],
            }

            service._submit_message(account, msg)

            self.assertEqual(len(service.executor.submissions), 0)
            self.assertEqual(len(service.command_executor.submissions), 1)

    def test_workspace_run_uses_codex_worker_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MultiWechatCodexService(make_test_config(tmp))
            service.executor = CapturingExecutor()
            service.command_executor = CapturingExecutor()
            account = {"accountId": "acct-1"}
            msg = {
                "message_type": MESSAGE_TYPE_USER,
                "from_user_id": "user-1",
                "context_token": "token-1",
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "/ws run a hello"}}],
            }

            service._submit_message(account, msg)

            self.assertEqual(len(service.executor.submissions), 1)
            self.assertEqual(len(service.command_executor.submissions), 0)

    def test_workspace_commands_add_use_list_and_run_by_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_a = Path(tmp) / "project-a"
            project_a.mkdir()
            service = MultiWechatCodexService(make_test_config(tmp))
            sent = []
            calls = []
            service._send_text = lambda account, user_id, text: sent.append(text)
            service._run_codex_and_reply = lambda account, user_id, key, text: calls.append((key, text))
            account = {"accountId": "acct-1"}
            base = service.state.conversation_key("acct-1", "user-1")

            service._handle_message(account, "user-1", base, f"/ws add a {project_a}")
            service._handle_message(account, "user-1", base, "/ws use a")
            service._handle_message(account, "user-1", base, "/ws")
            service._handle_message(account, "user-1", base, "/ws run a fix readme")

            self.assertEqual(service.state.get_active_workspace(base), "a")
            self.assertIn("a [idle]", sent[-1])
            self.assertIn(str(project_a), sent[-1])
            self.assertEqual(calls, [("acct-1:user-1:a", "fix readme")])

    def test_model_switch_updates_session_and_resets_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MultiWechatCodexService(make_test_config(tmp))
            service._model_options = [
                {"model": "gpt-5.5", "reasoningEffort": "medium", "label": "GPT-5.5"},
                {"model": "gpt-5.5", "reasoningEffort": "high", "label": "GPT-5.5"},
            ]
            sent = []
            service._send_text = lambda account, user_id, text: sent.append(text)
            account = {"accountId": "acct-1"}
            conversation_key = service.state.conversation_key("acct-1", "user-1")

            service._handle_model_switch(account, "user-1", conversation_key, "2")

            session = service.state.get_session(conversation_key, tmp)
            self.assertEqual(session["codexModel"], "gpt-5.5")
            self.assertEqual(session["codexReasoningEffort"], "high")
            self.assertEqual(session["codexThreadId"], "")
            self.assertIn("已经切换到 gpt-5.5:high", sent[-1])

    def test_models_command_lists_plain_model_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MultiWechatCodexService(make_test_config(tmp))
            service._model_options = [
                {"model": "gpt-5.5", "reasoningEffort": "low"},
                {"model": "gpt-5.5", "reasoningEffort": "medium"},
            ]
            sent = []
            service._send_text = lambda account, user_id, text: sent.append(text)
            account = {"accountId": "acct-1"}
            conversation_key = service.state.conversation_key("acct-1", "user-1")

            service._handle_model_switch(account, "user-1", conversation_key, "", list_only=True)

            self.assertEqual(
                sent[-1],
                "可切换模型（发送 /model 编号 切换）：\ngpt-5.5\n1. gpt-5.5:low\n2. gpt-5.5:medium",
            )

    def test_model_options_are_loaded_for_each_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MultiWechatCodexService(make_test_config(tmp))
            sent = []
            service._send_text = lambda account, user_id, text: sent.append(text)
            account = {"accountId": "acct-1"}
            conversation_key = service.state.conversation_key("acct-1", "user-1")

            with patch("wechat_codex_multi.service.model_options") as options:
                options.side_effect = [
                    [{"model": "gpt-live-a", "reasoningEffort": "low"}],
                    [{"model": "gpt-live-b", "reasoningEffort": "high"}],
                ]

                service._handle_model_switch(account, "user-1", conversation_key, "", list_only=True)
                service._handle_model_switch(account, "user-1", conversation_key, "", list_only=True)

            self.assertEqual(options.call_count, 2)
            self.assertIn("gpt-live-a:low", sent[0])
            self.assertIn("gpt-live-b:high", sent[1])

    def test_restart_requires_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MultiWechatCodexService(make_test_config(tmp))
            sent = []
            service._send_text = lambda account, user_id, text: sent.append(text)
            service._schedule_restart = lambda: sent.append("scheduled")
            account = {"accountId": "acct-1"}
            conversation_key = service.state.conversation_key("acct-1", "user-1")

            service._handle_message(account, "user-1", conversation_key, "/restart")

            self.assertEqual(sent, ["只有 adminUsers 可以通过微信触发 /restart。"])

    def test_restart_schedules_restart_for_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_test_config(tmp)
            config["adminUsers"] = ["user-1"]
            service = MultiWechatCodexService(config)
            sent = []
            service._send_text = lambda account, user_id, text: sent.append(text)
            service._schedule_restart = lambda: sent.append("scheduled")
            account = {"accountId": "acct-1"}
            conversation_key = service.state.conversation_key("acct-1", "user-1")

            service._handle_message(account, "user-1", conversation_key, "/restart")

            self.assertEqual(sent, ["正在重启服务，稍后可发送 /status 确认。", "scheduled"])

    def test_restart_can_run_without_conversation_lock(self):
        self.assertTrue(MultiWechatCodexService._can_run_without_conversation_lock("/restart"))

    def test_workspace_run_needs_conversation_lock(self):
        self.assertTrue(MultiWechatCodexService._can_run_without_conversation_lock("/ws"))
        self.assertFalse(MultiWechatCodexService._can_run_without_conversation_lock("/ws run a hello"))
        self.assertTrue(MultiWechatCodexService._can_run_without_conversation_lock("/runner"))

    def test_busy_conversation_queues_followup_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MultiWechatCodexService(make_test_config(tmp))
            sent = []
            service._send_text = lambda account, user_id, text: sent.append(text)
            account = {"accountId": "acct-1"}
            base = service.state.conversation_key("acct-1", "user-1")
            lock = service._conversation_lock(base)
            self.assertTrue(lock.acquire(blocking=False))
            try:
                service._handle_message_safe(account, "user-1", base, "补充：标题改短一点")
            finally:
                lock.release()

            self.assertEqual(service._pop_pending_guidance(base), ["补充：标题改短一点"])
            self.assertIn("已收到补充引导", sent[-1])

    def test_busy_conversation_uses_app_server_steer_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MultiWechatCodexService(make_test_config(tmp))
            service.codex = FakeSteerRunner(ok=True)
            sent = []
            service._send_text = lambda account, user_id, text: sent.append(text)
            account = {"accountId": "acct-1"}
            base = service.state.conversation_key("acct-1", "user-1")
            lock = service._conversation_lock(base)
            self.assertTrue(lock.acquire(blocking=False))
            try:
                service._handle_message_safe(account, "user-1", base, "补充：标题改短一点")
            finally:
                lock.release()

            self.assertEqual(service.codex.calls, [(base, "补充：标题改短一点")])
            self.assertEqual(service._pop_pending_guidance(base), [])
            self.assertIn("已发送引导", sent[-1])

    def test_busy_slash_command_is_not_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MultiWechatCodexService(make_test_config(tmp))
            service.codex = FakeSteerRunner(ok=True)
            sent = []
            service._send_text = lambda account, user_id, text: sent.append(text)
            account = {"accountId": "acct-1"}
            base = service.state.conversation_key("acct-1", "user-1")
            lock = service._conversation_lock(base)
            self.assertTrue(lock.acquire(blocking=False))
            try:
                service._handle_message_safe(account, "user-1", base, "/unknown")
            finally:
                lock.release()

            self.assertEqual(service.codex.calls, [])
            self.assertEqual(service._pop_pending_guidance(base), [])
            self.assertIn("命令不会作为引导处理", sent[-1])

    def test_runner_command_switches_runtime_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MultiWechatCodexService(make_test_config(tmp))
            fake = FakeSteerRunner(ok=True)
            service.codex = fake
            created = []
            service._create_codex_runner = lambda config: created.append(config["codex"]["runner"]) or FakeSteerRunner()
            sent = []
            service._send_text = lambda account, user_id, text: sent.append(text)
            account = {"accountId": "acct-1"}
            base = service.state.conversation_key("acct-1", "user-1")

            service._handle_message(account, "user-1", base, "/runner app-server")

            self.assertEqual(created, ["app-server"])
            self.assertIn(("terminate_all", ""), fake.calls)
            self.assertEqual(service.config["codex"]["runner"], "app-server")
            self.assertIn("已切换 Codex runner: app-server", sent[-1])

    def test_agent_command_switches_current_workspace_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MultiWechatCodexService(make_test_config(tmp))
            sent = []
            service._send_text = lambda account, user_id, text: sent.append(text)
            account = {"accountId": "acct-1"}
            base = service.state.conversation_key("acct-1", "user-1")

            service._handle_message(account, "user-1", base, "/agent claude")

            session = service.state.get_session(base, tmp)
            self.assertEqual(session["agent"], "claude")
            self.assertIn("已切换 Agent: claude", sent[-1])

    def test_agent_switch_is_rejected_while_workspace_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MultiWechatCodexService(make_test_config(tmp))
            service.codex = FakeSteerRunner(ok=True)
            sent = []
            service._send_text = lambda account, user_id, text: sent.append(text)
            account = {"accountId": "acct-1"}
            base = service.state.conversation_key("acct-1", "user-1")

            service._handle_message(account, "user-1", base, "/agent claude")

            session = service.state.get_session(base, tmp)
            self.assertNotEqual(session.get("agent"), "claude")
            self.assertIn("任务运行中", sent[-1])

    def test_model_switch_uses_claude_options_when_agent_is_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MultiWechatCodexService(make_test_config(tmp))
            service._claude_model_options = [
                {"model": "sonnet", "effort": "low"},
                {"model": "sonnet", "effort": "high"},
            ]
            sent = []
            service._send_text = lambda account, user_id, text: sent.append(text)
            account = {"accountId": "acct-1"}
            conversation_key = service.state.conversation_key("acct-1", "user-1")
            service.state.update_session(conversation_key, agent="claude", claudeSessionId="session-1")

            service._handle_model_switch(account, "user-1", conversation_key, "2")

            session = service.state.get_session(conversation_key, tmp)
            self.assertEqual(session["claudeModel"], "sonnet")
            self.assertEqual(session["claudeEffort"], "high")
            self.assertEqual(session["claudeSessionId"], "")
            self.assertIn("已经切换到 sonnet:high", sent[-1])

    def test_run_pending_guidance_continues_same_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MultiWechatCodexService(make_test_config(tmp))
            sent = []
            calls = []
            service._send_text = lambda account, user_id, text: sent.append(text)
            service._run_codex_and_reply = lambda account, user_id, key, text: calls.append((key, text))
            account = {"accountId": "acct-1"}
            base = service.state.conversation_key("acct-1", "user-1")

            service._append_pending_guidance(base, "第一条")
            service._append_pending_guidance(base, "第二条")
            service._run_pending_guidance(account, "user-1", base)

            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], base)
            self.assertIn("第一条", calls[0][1])
            self.assertIn("第二条", calls[0][1])
            self.assertIn("继续处理 2 条补充引导", sent[-1])

    def test_busy_interrupt_cancels_and_schedules_new_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MultiWechatCodexService(make_test_config(tmp))
            service.executor = CapturingExecutor()
            sent = []
            cancelled = []
            service._send_text = lambda account, user_id, text: sent.append(text)
            service.codex.cancel = lambda key: cancelled.append(key) or True
            account = {"accountId": "acct-1"}
            base = service.state.conversation_key("acct-1", "user-1")
            lock = service._conversation_lock(base)
            self.assertTrue(lock.acquire(blocking=False))
            try:
                service._handle_message_safe(account, "user-1", base, "/interrupt 改做新任务")
            finally:
                lock.release()

            self.assertEqual(cancelled, [base])
            self.assertIn("已中断当前 Codex 任务", sent[-1])
            self.assertEqual(len(service.executor.submissions), 1)


if __name__ == "__main__":
    unittest.main()
