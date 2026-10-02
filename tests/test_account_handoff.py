import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_desktop_codex as desktop_tests
import test_local_ux as ux_tests
from wechat_codex_multi.account_registry import CodexAccountRegistry
from wechat_codex_multi.commands import normalize_command
from wechat_codex_multi.config import load_config
from wechat_codex_multi.project_handoff import build_handoff, local_transcript


class AccountRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "config.json"
        self.raw = {"stateDir": self.tmp.name, "adminUsers": ["admin"], "privateSetting": {"keep": True},
                    "codex": {"workingDirectory": self.tmp.name, "accountsDirectory": self.tmp.name + "/homes",
                              "accounts": [{"name": "main", "codexHome": self.tmp.name + "/main"}]}}
        self.file.write_text(json.dumps(self.raw))
        self.config = load_config(self.file)
        self.registry = CodexAccountRegistry(self.config)

    def test_register_persists_account_and_creates_private_directory_idempotently(self):
        account, created = self.registry.register("backup3")
        self.assertTrue(created)
        self.assertTrue(Path(account["codexHome"]).is_dir())
        self.assertEqual(Path(account["codexHome"]).stat().st_mode & 0o777, 0o700)
        disk = json.loads(self.file.read_text())
        self.assertEqual(disk["privateSetting"], self.raw["privateSetting"])
        self.assertEqual(disk["adminUsers"], ["admin"])
        self.assertEqual(self.registry.register("backup3"), (account, False))
        self.assertEqual([item["name"] for item in load_config(self.file)["codex"]["accounts"]], ["main", "backup3"])
        self.assertFalse((Path(account["codexHome"]) / "auth.json").exists())

    def test_manual_addition_updates_shared_runner_list_but_other_settings_wait_for_restart(self):
        shared_list = self.config["codex"]["accounts"]
        self.raw["adminUsers"] = ["different-admin"]
        self.raw["codex"]["accounts"].append({"name": "new", "codexHome": self.tmp.name + "/new"})
        self.file.write_text(json.dumps(self.raw))
        self.registry.refresh()
        self.assertIs(self.config["codex"]["accounts"], shared_list)
        self.assertEqual([item["name"] for item in shared_list], ["main", "new"])
        self.assertEqual(self.config["adminUsers"], ["admin"])

    def test_invalid_json_or_home_changes_leave_live_accounts_unchanged(self):
        original = copy.deepcopy(self.config["codex"]["accounts"])
        self.file.write_text("{")
        with self.assertRaises(ValueError):
            self.registry.register("backup3")
        self.assertEqual(self.config["codex"]["accounts"], original)
        self.raw["codex"]["accounts"][0]["codexHome"] = self.tmp.name + "/other"
        self.file.write_text(json.dumps(self.raw))
        with self.assertRaisesRegex(ValueError, "需要重启"):
            self.registry.refresh()
        self.assertEqual(self.config["codex"]["accounts"], original)

    def test_invalid_names_and_shared_or_escaping_directories_are_rejected(self):
        for value in ("../bad", "/tmp/bad", "1", "bad name", "a;cmd", "a" * 65):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.registry.register(value)
        root = Path(self.tmp.name) / "homes"
        root.mkdir()
        (root / "escape").symlink_to(Path(self.tmp.name))
        with self.assertRaises(ValueError):
            self.registry.register("escape")
        self.raw["codex"]["accounts"].append({"name": "same", "codexHome": self.tmp.name + "/main"})
        self.file.write_text(json.dumps(self.raw))
        with self.assertRaisesRegex(ValueError, "同一目录"):
            self.registry.refresh()

    def test_concurrent_registration_preserves_both_accounts(self):
        registries = [self.registry, CodexAccountRegistry(load_config(self.file))]
        errors = []
        def register(registry, name):
            try:
                registry.register(name)
            except Exception as error:
                errors.append(error)
        workers = [threading.Thread(target=register, args=(registries[i], name))
                   for i, name in enumerate(("backup3", "backup4"))]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual({item["name"] for item in load_config(self.file)["codex"]["accounts"]},
                         {"main", "backup3", "backup4"})


class AccountHandoffTests(unittest.TestCase):
    tearDown = ux_tests.LocalUxTests.tearDown
    drain = ux_tests.LocalUxTests.drain
    command = ux_tests.LocalUxTests.command
    submit = ux_tests.LocalUxTests.submit
    latest = ux_tests.LocalUxTests.latest

    def setUp(self):
        ux_tests.LocalUxTests.setUp(self)
        for agent in ("codex", "claude"):
            field = "codexHome" if agent == "codex" else "claudeConfigDir"
            self.service.config[agent]["accounts"] = [
                {"name": name, field: self.tmp.name + "/" + agent + "-" + name}
                for name in ("main", "backup", "third")]
        self.service.config["adminUsers"] = ["user-1"]
        self.service.config["codex"]["accountsDirectory"] = self.tmp.name + "/new-homes"
        self.file = Path(self.tmp.name) / "config.json"
        self.file.write_text(json.dumps(self.service.config))
        self.service.config["configFile"] = str(self.file)
        self.service.account_registry = CodexAccountRegistry(self.service.config)

    def test_switch_preserves_directory_creates_fresh_session_and_injects_context_once(self):
        self.service.state.update_session(self.owner, codexThreadId="old-thread")
        self.command("原始要求：完成按钮交互")
        self.runs.clear()
        self.command("/codex backup")
        session = self.service._get_session(self.owner)
        self.assertEqual(session["cwd"], self.tmp.name)
        self.assertEqual(session["codexThreadId"], "")
        self.assertEqual(session["codexAccount"], "backup")
        self.assertFalse(self.runs)
        self.command("继续处理")
        self.assertIn("原始要求：完成按钮交互", self.runs[0][1])
        self.assertIn("完成结果", self.runs[0][1])
        self.assertEqual(self.latest()["prompt"], "继续处理")
        self.assertIsNone(self.service._get_session(self.owner).get("codexAccountHandoff"))
        self.command("再检查一下")
        self.assertEqual(self.runs[-1][1], "再检查一下")

    def test_switch_back_uses_current_project_context_and_never_restores_old_account_session(self):
        self.command("A 账号任务")
        self.command("/codex backup")
        self.command("B 账号的新要求")
        self.service.state.update_session(self.owner, codexThreadId="b-thread")
        self.command("/codex main")
        self.assertEqual(self.service._get_session(self.owner)["codexThreadId"], "")
        self.assertIn("保留原项目目录", self.sent[-1])

    def test_switch_chain_before_execution_preserves_context_without_recursive_embedding(self):
        self.command("项目最初的要求")
        self.command("/codex backup")
        self.command("/codex third")
        self.command("继续")
        self.assertIn("项目最初的要求", self.runs[-1][1])
        self.assertEqual(self.runs[-1][1].count("[本地项目交接："), 1)

    def test_failure_keeps_handoff_for_retry(self):
        self.command("先前任务")
        self.command("/codex backup")
        original_run = self.service.codex.run
        self.service.codex.run = Mock(side_effect=RuntimeError("尚未登录"))
        self.command("继续")
        self.assertIsNotNone(self.service._get_session(self.owner)["codexAccountHandoff"])
        self.service.codex.run = original_run
        self.command("重试处理")
        self.assertIn("先前任务", self.runs[-1][1])
        self.assertIsNone(self.service._get_session(self.owner)["codexAccountHandoff"])

    def test_original_requirements_survive_multiple_completed_account_handoffs(self):
        self.command("原始约束：保持中文菜单且不要修改数据库")
        self.command("/codex backup")
        self.command("继续完成第一部分")
        self.command("/codex third")
        self.command("继续完成第二部分")
        self.assertIn("原始约束：保持中文菜单且不要修改数据库", self.runs[-1][1])
        self.assertEqual(self.runs[-1][1].count("[本地项目交接："), 1)

    def test_reset_or_explicit_session_selection_discards_pending_handoff(self):
        self.command("先前任务")
        self.command("/codex backup")
        self.command("/reset")
        self.assertIsNone(self.service._get_session(self.owner)["codexAccountHandoff"])
        self.command("/codex main")
        self.command("下一件事")
        self.assertNotIn("先前任务", self.runs[-1][1])

    def test_handoff_is_scoped_to_current_user_workspace_and_agent(self):
        self.command("另一个用户的私有任务", user="user-2")
        self.service.state.upsert_workspace(self.owner, "other", self.tmp.name)
        self.command("/ws run other 另一个项目的私有任务")
        self.command("本项目要求")
        self.command("/codex backup")
        self.command("继续")
        self.assertIn("本项目要求", self.runs[-1][1])
        self.assertNotIn("另一个用户", self.runs[-1][1])
        self.assertNotIn("另一个项目", self.runs[-1][1])

    def test_claude_account_handoff_is_separate_from_codex_context(self):
        self.command("Codex 的任务")
        self.service.state.update_session(self.owner, agent="claude")
        self.command("Claude 的任务")
        self.command("/account backup")
        self.command("继续 Claude")
        self.assertIn("Claude 的任务", self.runs[-1][1])
        self.assertNotIn("Codex 的任务", self.runs[-1][1])

    def test_disabled_handoff_retains_original_account_switch_behavior(self):
        self.service.config["handoff"] = {"enabled": False}
        self.command("旧任务")
        self.command("/codex backup")
        self.command("新任务")
        self.assertEqual(self.runs[-1][1], "新任务")

    def test_desktop_switch_reads_native_history_and_registers_same_directory_on_target(self):
        fake = desktop_tests.FakeDesktop()
        self.service.desktop = fake
        self.command("/d-sessions all")
        self.command("/d-session 1")
        self.command("/codex backup")
        fake.project_list = []
        self.command("接着处理")
        self.assertIn("用户提问", self.runs[-1][1])
        self.assertIn("最终回答", self.runs[-1][1])
        self.assertEqual(self.service._get_session(self.owner)["codexAccount"], "backup")
        self.assertEqual(self.service._get_session(self.owner)["cwd"], "/tmp/project")
        self.assertTrue(any(item[0] == "project/create" for item in fake.calls))
        self.assertIsNone(self.service._get_session(self.owner)["codexAccountHandoff"])

    def test_new_desktop_thread_keeps_handoff_when_pending_key_becomes_a_real_thread(self):
        self.service.desktop = desktop_tests.FakeDesktop()
        self.command("/d-sessions all")
        self.command("/d-session 1")
        self.command("/codex backup")
        def finish(key, text):
            self.runs.append((key, text))
            self.service.state.update_session(key, codexThreadId="new-backup-thread")
            return "处理完成"
        self.service.codex.run = finish
        self.command("B 账号新增的要求：保留原数据")
        self.assertEqual(self.service._get_session(self.owner)["codexThreadId"], "new-backup-thread")
        self.command("/codex third")
        packet = self.service._get_session(self.owner)["codexAccountHandoff"]
        self.assertIn("用户提问", packet["text"])
        self.assertIn("B 账号新增的要求：保留原数据", packet["text"])
        self.assertEqual(packet["text"].count("[本地项目交接："), 1)

        self.command("C 账号的新进度")
        self.command("/codex main")
        packet = self.service._get_session(self.owner)["codexAccountHandoff"]
        self.assertIn("用户提问", packet["text"])
        self.assertIn("C 账号的新进度", packet["text"])

    def test_desktop_browsing_keeps_execution_target_until_new_session_is_selected(self):
        self.service.desktop = desktop_tests.FakeDesktop()
        self.command("/d-sessions all")
        self.command("/d-session 1")
        original = self.service._get_session(self.owner)
        self.command("/d-account backup")
        self.assertEqual(self.service._get_session(self.owner)["codexThreadId"], original["codexThreadId"])
        self.assertEqual(self.service._get_session(self.owner)["codexAccount"], "main")
        self.command("/d-session new")
        self.assertEqual(self.service._get_session(self.owner)["codexAccount"], "backup")
        self.assertIsNotNone(self.service._get_session(self.owner)["codexAccountHandoff"])

    def test_desktop_account_switch_is_rejected_while_old_task_is_queued(self):
        self.service.desktop = desktop_tests.FakeDesktop()
        self.command("/d-sessions all")
        self.command("/d-session 1")
        self.submit("等待执行")
        self.command("/codex backup")
        self.assertIn("排队中", self.sent[-1])
        self.assertEqual(self.service._get_session(self.owner)["codexAccount"], "main")

    def test_login_registers_unknown_account_and_preserves_wechat_login_syntax(self):
        with patch.object(self.service.codex_device_login, "start", return_value=True) as start, \
                patch("wechat_codex_multi.service.login_with_qr") as qr:
            self.command("/login backup3 person@example.com")
        start.assert_called_once()
        self.assertEqual(start.call_args.kwargs["expected_email"], "person@example.com")
        qr.assert_not_called()
        disk = json.loads(self.file.read_text())
        self.assertIn("backup3", [item["name"] for item in disk["codex"]["accounts"]])
        self.assertTrue((Path(self.tmp.name) / "new-homes" / "backup3").is_dir())
        self.assertEqual(normalize_command("/login 微信昵称"), "/login 微信昵称")
        self.assertEqual(normalize_command("/login"), "/login")
        self.assertFalse(self.runs)

    def test_login_status_cancel_and_nonadmin_never_create_accounts(self):
        before = self.file.read_text()
        with patch.object(self.service.codex_device_login, "start") as start:
            self.command("/codex-login backup3 person@example.com", user="user-2")
            self.command("/codex-login status backup3")
            self.command("/codex-login cancel backup3")
            self.command("/codex-login ../escape person@example.com")
            self.command("/codex-login backup3 not-an-email")
        self.assertEqual(self.file.read_text(), before)
        start.assert_not_called()

    def test_manual_account_addition_is_available_on_next_command(self):
        raw = json.loads(self.file.read_text())
        raw["codex"]["accounts"].append({"name": "manual", "codexHome": self.tmp.name + "/manual"})
        self.file.write_text(json.dumps(raw))
        self.command("/codex manual")
        self.assertEqual(self.service._get_session(self.owner)["codexAccount"], "manual")

    def test_account_login_is_rejected_while_task_is_queued(self):
        self.submit("排队中的任务")
        with patch.object(self.service.codex_device_login, "start") as start:
            self.command("/codex-login main person@example.com")
        start.assert_not_called()
        self.assertIn("排队中", self.sent[-1])

    def test_task_waits_for_account_login_without_launching_an_agent_or_losing_handoff(self):
        self.command("原始任务")
        self.command("/codex backup")
        self.runs.clear()
        with patch.object(self.service.codex_device_login, "is_running", return_value=True):
            self.command("继续")
        self.assertFalse(self.runs)
        self.assertIn("正在登录", self.sent[-1])
        self.assertIsNotNone(self.service._get_session(self.owner)["codexAccountHandoff"])

    def test_pending_guidance_can_be_cancelled_before_its_agent_process_starts(self):
        typing_calls, stopped = [], []
        def run(key, text):
            self.runs.append((key, text))
            self.service._append_pending_guidance(key, "稍后处理的补充")
            return "第一项完成"
        def typing(*_args):
            typing_calls.append(True)
            if len(typing_calls) == 2:
                stopped.append(self.service._cancel_runner(self.owner, reset_session=False))
            return lambda: None
        self.service.codex.run = run
        self.service._start_typing_loop = typing
        self.command("第一项任务")
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(stopped, [True])
        self.assertEqual(self.latest()["status"], "cancelled")

    def test_overspecified_login_alias_does_not_create_a_wechat_bot(self):
        with patch.object(self.service.codex_device_login, "start") as start, \
                patch("wechat_codex_multi.service.login_with_qr") as qr:
            self.command("/login backup3 person@example.com unexpected")
        start.assert_not_called()
        qr.assert_not_called()
        self.assertIn("用法", self.sent[-1])


class LocalTranscriptTests(unittest.TestCase):
    def test_selected_codex_rollout_is_read_without_copying_auth_or_other_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            sessions = home / "sessions"
            sessions.mkdir()
            data = [{"type": "session_meta", "payload": {"id": "selected", "cwd": tmp}},
                    {"type": "response_item", "payload": {"role": "user", "content": [{"text": "原始需求"}]}},
                    {"type": "response_item", "payload": {"role": "assistant", "phase": "final_answer",
                                                              "content": [{"text": "已完成部分"}]}}]
            (sessions / "rollout-selected.jsonl").write_text("\n".join(json.dumps(item) for item in data))
            (home / "auth.json").write_text("private credentials")
            records = local_transcript({"codexHome": tmp}, "codex", "selected", tmp)
            self.assertEqual(records[0]["prompt"], "原始需求")
            self.assertEqual(records[0]["result"], "已完成部分")
            self.assertEqual(local_transcript({"codexHome": tmp}, "codex", "other", tmp), [])
            self.assertEqual(local_transcript({"codexHome": tmp}, "codex", "selected", "/different"), [])
            self.assertEqual(local_transcript({"codexHome": tmp}, "codex", "../auth", tmp), [])
            self.assertEqual((home / "auth.json").read_text(), "private credentials")

    def test_packet_is_bounded_and_does_not_replay_media_markers(self):
        with patch("wechat_codex_multi.project_handoff._changes", return_value=""):
            packet = build_handoff({"cwd": "/project"}, "codex", "main", "backup",
                                   [{"prompt": "x" * 20000, "result": "y" * 20000 + "\n[[send_file:/tmp/a.pdf]]"}], 1000)
        self.assertLessEqual(len(packet["text"]), 1000)
        self.assertIn("截断", packet["text"])
        self.assertNotIn("[[send_file:", packet["text"])


if __name__ == "__main__":
    unittest.main()
