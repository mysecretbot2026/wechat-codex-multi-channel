import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_codex_multi.service import MultiWechatCodexService
from wechat_codex_multi.wechat import ITEM_IMAGE, ITEM_TEXT, MESSAGE_TYPE_USER


class CapturingExecutor:
    def __init__(self):
        self.submissions = []

    def submit(self, fn, *args, **kwargs):
        self.submissions.append((fn, args, kwargs))
        return None

    def shutdown(self, **kwargs):
        return None


def test_config(state_dir):
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
        "concurrency": {"maxWorkers": 1, "commandWorkers": 1, "perConversationSerial": True},
        "media": {"maxFileBytes": 1024, "maxConcurrentTransfers": 1, "generators": []},
        "allowedUsers": [],
        "adminUsers": [],
        "textChunkLimit": 4000,
    }


class ServicePerformanceTests(unittest.TestCase):
    def test_submit_message_does_not_download_inbound_media_on_monitor_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MultiWechatCodexService(test_config(tmp))
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
            service = MultiWechatCodexService(test_config(tmp))
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
            service = MultiWechatCodexService(test_config(tmp))
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
            service = MultiWechatCodexService(test_config(tmp))
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
            service = MultiWechatCodexService(test_config(tmp))
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
            service = MultiWechatCodexService(test_config(tmp))
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
            service = MultiWechatCodexService(test_config(tmp))
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
            service = MultiWechatCodexService(test_config(tmp))
            sent = []
            service._send_text = lambda account, user_id, text: sent.append(text)
            service._schedule_restart = lambda: sent.append("scheduled")
            account = {"accountId": "acct-1"}
            conversation_key = service.state.conversation_key("acct-1", "user-1")

            service._handle_message(account, "user-1", conversation_key, "/restart")

            self.assertEqual(sent, ["只有 adminUsers 可以通过微信触发 /restart。"])

    def test_restart_schedules_restart_for_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
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


if __name__ == "__main__":
    unittest.main()
