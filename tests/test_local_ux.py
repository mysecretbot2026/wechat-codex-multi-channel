import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_desktop_codex as desktop_tests
from test_service_performance import CapturingExecutor, FakeRunRunner, FakeSteerRunner, make_test_config
from wechat_codex_multi.media_outbox import media_outbox_path, queue_media, read_media_outbox
from wechat_codex_multi.service import MultiWechatCodexService
from wechat_codex_multi.task_journal import TaskJournal
from wechat_codex_multi.wechat import ITEM_TEXT, MESSAGE_TYPE_USER


class LocalUxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = make_test_config(self.tmp.name)
        config["notifications"] = {"taskReceipts": True, "retryFailedDeliveriesOnMessage": True}
        self.service = MultiWechatCodexService(config)
        for name in ("executor", "command_executor", "notification_executor"):
            getattr(self.service, name).shutdown(wait=True)
            setattr(self.service, name, CapturingExecutor())
        self.service.codex = FakeRunRunner()
        self.sent = []
        self.runs = []
        self.service._send_text = lambda account, user, text: self.sent.append(text)
        self.service._start_typing_loop = lambda *args: lambda: None
        self.service.codex.run = lambda key, text: self.runs.append((key, text)) or "完成结果"
        self.account = {"accountId": "bot-1"}
        self.owner = "bot-1:user-1"
        self.service.state.set_context_token("bot-1", "user-1", "context")

    def tearDown(self):
        self.service.stop()
        self.tmp.cleanup()

    def submit(self, text, user="user-1"):
        self.service._submit_message(self.account, {
            "message_type": MESSAGE_TYPE_USER, "from_user_id": user, "context_token": "context",
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        })

    def drain(self, executor):
        while executor.submissions:
            fn, args, kwargs = executor.submissions.pop(0)
            fn(*args, **kwargs)

    def command(self, text, user="user-1"):
        self.service._handle_message_safe(self.account, user, f"bot-1:{user}", text)

    def latest(self):
        return self.service.tasks.list(self.owner)[0]

    def test_unknown_and_malformed_commands_never_call_agent(self):
        for text in ("/stats", "/cwd-typo /tmp", "/status extra", "/help missing", "/guide text"):
            with self.subTest(text=text):
                self.command(text)
        self.assertFalse(self.runs)
        self.assertIn("/status", self.sent[0])
        self.assertIn("未知命令", self.sent[0])
        self.assertEqual(self.service.tasks.list(self.owner), [])

    def test_receipt_is_local_and_precedes_execution(self):
        self.submit("整理这个项目")
        self.assertFalse(self.runs)
        self.assertEqual(self.latest()["status"], "queued")
        self.drain(self.service.notification_executor)
        self.assertIn("任务已接收", self.sent[-1])
        self.assertIn(self.latest()["id"], self.sent[-1])
        self.drain(self.service.executor)
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.latest()["status"], "completed")
        self.assertEqual(self.sent[-1], "完成结果")

    def test_queued_task_keeps_workspace_after_switch(self):
        for name in ("a", "b"):
            self.service.state.upsert_workspace(self.owner, name, self.tmp.name)
        self.service.state.set_active_workspace(self.owner, "a")
        self.submit("这是 A 的任务")
        self.command("/切换项目 b")
        self.drain(self.service.notification_executor)
        self.drain(self.service.executor)
        self.assertEqual(self.runs, [(self.owner + ":a", "这是 A 的任务")])
        self.assertEqual(self.service.state.get_active_workspace(self.owner), "b")
        self.assertIn("后台任务", self.sent[-1])
        self.assertTrue(self.latest()["unread"])

    def test_queued_session_cannot_be_replaced_and_can_be_cancelled(self):
        self.service.state.update_session(self.owner, codexThreadId="original-session")
        self.submit("等待中的任务")
        self.command("/session new")
        self.assertIn("排队中", self.sent[-1])
        self.assertEqual(self.service._get_session(self.owner)["codexThreadId"], "original-session")

        self.command("/停止")
        self.drain(self.service.executor)
        self.assertFalse(self.runs)
        self.assertEqual(self.latest()["status"], "cancelled")
        self.assertEqual(self.service._get_session(self.owner)["codexThreadId"], "original-session")

    def test_changed_queued_target_is_rejected_before_agent_launch(self):
        self.service.state.update_session(self.owner, codexThreadId="original-session")
        self.submit("等待中的任务")
        # Simulate a concurrent command that began before the receipt was created.
        self.service.state.update_session(self.owner, codexThreadId="different-session")
        self.drain(self.service.notification_executor)
        self.drain(self.service.executor)
        self.assertFalse(self.runs)
        self.assertEqual(self.latest()["status"], "failed")
        self.assertIn("目标会话设置发生变化", self.latest()["error"])

    def test_queued_task_cannot_change_agent_or_switch_cli_into_desktop_route(self):
        self.submit("等待执行")
        for command in ("/claude", "/agent claude", "/d-session new", "/d-session use 1"):
            with self.subTest(command=command):
                self.command(command)
                self.assertIn("排队中", self.sent[-1])
        self.assertEqual(self.service._get_session(self.owner)["agent"], "codex")
        self.assertNotEqual(self.service._get_session(self.owner).get("codexClient"), "desktop")

    def test_explicit_workspace_task_keeps_desktop_session_after_selection_changes(self):
        fake = desktop_tests.FakeDesktop()
        self.service.desktop = fake
        self.command("/d-sessions all")
        self.command("/d-session 1")
        original = self.service._desktop_execution_key(self.owner)
        self.submit("/ws run default 这是 A 会话的任务")
        other = dict(fake.thread, id="thread-other", title="另一个会话")
        fake.threads = lambda account, archived=False: [dict(fake.thread), other]
        self.command("/d-sessions all")
        self.command("/d-session 2")
        self.drain(self.service.notification_executor)
        self.drain(self.service.executor)
        self.assertEqual(self.runs, [(original, "这是 A 会话的任务")])
        self.assertEqual(self.service._get_session(self.owner)["codexThreadId"], "thread-other")
        self.assertTrue(self.latest()["background"])

    def test_late_guidance_for_cancelled_queued_task_never_starts_an_agent(self):
        self.submit("原任务")
        self.submit("稍后才处理到的补充")
        self.command("/停止")
        self.drain(self.service.command_executor)
        self.drain(self.service.executor)
        self.assertFalse(self.runs)
        self.assertFalse(self.service._pop_pending_guidance(self.owner))
        self.assertIn("补充不会继续执行", self.sent[-1])

    def test_cancel_during_startup_prevents_agent_launch(self):
        self.submit("准备启动的任务")
        self.service._start_typing_loop = lambda *args: self.service._cancel_runner(self.owner, reset_session=False) and (lambda: None)
        self.drain(self.service.executor)
        self.assertFalse(self.runs)
        self.assertEqual(self.latest()["status"], "cancelled")

    def test_interrupt_with_new_task_uses_task_pool_and_preserves_session(self):
        self.service.state.update_session(self.owner, codexThreadId="original-session")
        self.submit("原任务")
        self.command("/停止 改做这件事")
        self.assertFalse(self.runs)
        self.assertEqual(len(self.service.executor.submissions), 2)
        self.drain(self.service.notification_executor)
        self.drain(self.service.executor)
        self.assertEqual(self.runs, [(self.owner, "改做这件事")])
        self.assertEqual(self.service._get_session(self.owner)["codexThreadId"], "original-session")
        self.assertEqual({item["status"] for item in self.service.tasks.list(self.owner)}, {"completed", "cancelled"})

    def test_replacement_task_keeps_session_id_published_by_interrupted_task(self):
        self.submit("原任务")
        self.command("/停止 改做这件事")
        self.service.state.update_session(self.owner, codexThreadId="just-created-session")
        self.drain(self.service.notification_executor)
        self.drain(self.service.executor)
        self.assertEqual(self.runs, [(self.owner, "改做这件事")])
        self.assertEqual(self.latest()["sessionId"], "just-created-session")

    def test_running_guidance_bypasses_saturated_task_executor(self):
        runner = FakeSteerRunner()
        self.service.codex = runner
        self.submit("/补充 把标题缩短")
        self.assertFalse(self.service.executor.submissions)
        self.drain(self.service.command_executor)
        self.assertEqual(runner.calls, [(self.owner, "把标题缩短")])
        self.assertIn("已发送引导", self.sent[-1])
        self.assertEqual(self.service.tasks.list(self.owner), [])

    def test_guidance_received_while_queued_is_kept_for_that_task(self):
        self.submit("第一项任务")
        self.submit("补充这个任务的要求")
        self.drain(self.service.command_executor)
        self.drain(self.service.notification_executor)
        self.drain(self.service.executor)
        self.assertEqual(len(self.runs), 2)
        self.assertEqual(self.runs[0], (self.owner, "第一项任务"))
        self.assertIn("补充这个任务的要求", self.runs[1][1])

    def test_menu_and_number_selection_do_not_call_agent(self):
        self.submit("菜单")
        self.drain(self.service.command_executor)
        self.assertIn("中文菜单", self.sent[-1])
        self.submit("4")
        self.drain(self.service.command_executor)
        self.assertIn("最近任务", self.sent[-1])
        self.assertFalse(self.runs)
        self.assertFalse(self.service.executor.submissions)

    def test_menu_expiry_and_exit_are_local_but_numbers_outside_menu_are_prompts(self):
        self.command("菜单")
        self.service.menu_choices[self.owner]["expires"] = time.monotonic() - 1
        self.command("4")
        self.assertIn("菜单已过期", self.sent[-1])
        self.command("菜单")
        self.command("0")
        self.assertIn("已退出菜单", self.sent[-1])
        self.command("4")
        self.assertEqual(self.runs, [(self.owner, "4")])

    def test_menu_stop_controls_a_running_task(self):
        cancelled = []
        self.service.codex.cancel = lambda key, reset_session=True: cancelled.append((key, reset_session)) or True
        self.command("菜单")
        self.command("8")
        self.assertEqual(cancelled, [(self.owner, False)])
        self.assertFalse(self.runs)

    def test_notification_preference_survives_reload(self):
        self.command("/提醒 关闭")
        self.assertFalse(self.service._background_notifications_enabled(self.owner))
        self.assertFalse(type(self.service.state)(self.tmp.name).user_preferences(self.owner)["backgroundCompletion"])
        self.service._deliver_agent_output(self.account, "user-1", self.owner, "后台内容", background=True)
        self.assertNotIn("后台内容", self.sent[-1])
        self.assertTrue(self.latest()["unread"])
        self.command("/结果")
        self.assertEqual(self.sent[-1], "后台内容")
        self.assertFalse(self.latest()["unread"])

    def test_background_result_is_saved_and_notifies_only_once(self):
        self.service._deliver_agent_output(self.account, "user-1", self.owner, "后台完成的文字", background=True)
        task = self.latest()
        self.assertIn("后台任务", self.sent[-1])
        self.assertTrue(task["unread"])
        self.assertFalse(task["chunks"][0]["sent"])
        count = len(self.sent)
        self.service._notify_background_task(self.account, "user-1", task)
        self.assertEqual(len(self.sent), count)
        self.command("/任务 未读")
        self.assertIn(task["id"], self.sent[-1])
        self.command("/结果 " + task["id"])
        self.assertEqual(self.sent[-1], "后台完成的文字")
        self.assertFalse(self.latest()["unread"])
        self.assertFalse(self.runs)

    def test_background_failure_notifies_and_keeps_current_project(self):
        self.service.state.upsert_workspace(self.owner, "other", self.tmp.name)
        def fail(key, text):
            self.service.state.set_active_workspace(self.owner, "other")
            raise RuntimeError("账号登录已过期")
        self.service.codex.run = fail
        self.command("执行任务")
        self.assertIn("后台任务", self.sent[-1])
        self.assertIn("失败", self.sent[-1])
        self.assertEqual(self.latest()["status"], "failed")
        self.assertEqual(self.service.state.get_active_workspace(self.owner), "other")

    def test_empty_completed_output_has_a_local_completion_message(self):
        self.service._deliver_agent_output(self.account, "user-1", self.owner, "")
        self.assertIn("已完成，本轮没有可发送", self.sent[-1])
        self.assertFalse(self.latest()["unread"])
        self.assertFalse(self.runs)

    def test_partial_text_delivery_retries_only_unsent_chunks_after_reload(self):
        self.service.config["textChunkLimit"] = 8
        failed_once = [False]
        def send(account, user, text):
            if text == "第二段文字。" and not failed_once[0]:
                failed_once[0] = True
                raise RuntimeError("网络中断")
            self.sent.append(text)
        self.service._send_text = send
        self.service._deliver_agent_output(self.account, "user-1", self.owner, "第一段文字。\n第二段文字。")
        task_id = self.latest()["id"]
        self.assertTrue(self.latest()["chunks"][0]["sent"])
        self.assertFalse(self.latest()["chunks"][1]["sent"])
        self.service.tasks = TaskJournal(self.tmp.name)
        self.command("/重发 " + task_id)
        self.assertEqual(self.sent.count("第一段文字。"), 1)
        self.assertEqual(self.sent.count("第二段文字。"), 1)
        self.assertEqual(self.latest()["deliveryError"], "")
        self.assertFalse(self.runs)

    def test_partial_media_delivery_keeps_unsent_file_and_automatically_retries(self):
        files = [(Path(self.tmp.name) / name).resolve() for name in ("one.pdf", "two.pdf")]
        for path in files:
            path.write_bytes(b"pdf")
        outbox = media_outbox_path(self.tmp.name, self.owner)
        queue_media(outbox, [str(path) for path in files])
        calls = []
        def send(*args, **kwargs):
            path = args[3][0]["path"]
            calls.append(path)
            if path == str(files[1]) and calls.count(path) == 1:
                raise RuntimeError("第二份附件上传失败")
            return [path]
        with patch("wechat_codex_multi.service.execute_actions", side_effect=send):
            self.service._deliver_agent_output(self.account, "user-1", self.owner, "文件已经生成")
            self.assertEqual([item["path"] for item in read_media_outbox(outbox)], [str(files[1])])
            self.assertEqual(self.latest()["status"], "completed")
            self.assertTrue(self.latest()["deliveryError"])
            self.submit("/状态")
            self.drain(self.service.notification_executor)
        self.assertEqual(calls.count(str(files[0])), 1)
        self.assertEqual(calls.count(str(files[1])), 2)
        self.assertEqual(read_media_outbox(outbox), [])
        self.assertFalse(self.latest()["deliveryError"])
        self.assertFalse(self.runs)

    def test_unsent_old_outbox_is_not_reassigned_to_a_new_task(self):
        file = Path(self.tmp.name) / "pending.pdf"
        file.write_bytes(b"pdf")
        outbox = media_outbox_path(self.tmp.name, self.owner)
        queue_media(outbox, [str(file)])
        with patch("wechat_codex_multi.service.execute_actions", side_effect=RuntimeError("offline")):
            self.service._deliver_agent_output(self.account, "user-1", self.owner, "第一个结果")
        old_id = self.latest()["id"]
        self.service._deliver_agent_output(self.account, "user-1", self.owner, "第二个结果")
        self.assertEqual(self.latest()["media"], [])
        self.assertEqual(len(self.service.tasks.get(old_id)["media"]), 1)
        self.assertEqual(len(read_media_outbox(outbox)), 1)

    def test_background_attachment_is_sent_on_result_request_and_never_replayed(self):
        file = Path(self.tmp.name) / "result.pdf"
        file.write_bytes(b"pdf")
        queue_media(media_outbox_path(self.tmp.name, self.owner), [str(file)])
        with patch("wechat_codex_multi.service.execute_actions", return_value=[str(file)]) as send:
            self.service._deliver_agent_output(self.account, "user-1", self.owner, "后台文件", background=True)
            send.assert_not_called()
            self.command("/结果")
            send.assert_called_once()
            self.command("/结果")
            self.command("/重发")
            self.assertEqual(send.call_count, 1)
        self.assertFalse(self.runs)

    def test_other_user_cannot_list_read_or_resend_task(self):
        self.service._deliver_agent_output(self.account, "user-1", self.owner, "私有结果")
        task_id = self.latest()["id"]
        for text in ("/任务", "/结果 " + task_id, "/重发 " + task_id):
            self.command(text, user="user-2")
            self.assertNotIn("私有结果", self.sent[-1])
            self.assertNotIn(task_id, self.sent[-1])
        self.assertFalse(self.service._send_task_output(self.account, "user-2", task_id))

    def test_restart_does_not_automatically_rerun_unfinished_tasks(self):
        self.submit("运行前重启")
        journal = TaskJournal(self.tmp.name)
        self.assertEqual(journal.list(self.owner)[0]["status"], "interrupted")
        self.assertFalse(self.runs)

    def test_help_documents_local_interactions(self):
        for full in (False, True):
            help_text = self.service._help_text("bot-1", full=full)
            for command in ("/menu", "/任务", "/结果", "/重发", "/提醒"):
                self.assertIn(command, help_text)
            self.assertIn("不调用模型", help_text)


if __name__ == "__main__":
    unittest.main()
