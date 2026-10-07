import copy
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wechat_codex_multi.codex_device_login import CodexDeviceLoginManager, login_status
from wechat_codex_multi.config import DEFAULT_CONFIG
from wechat_codex_multi.service import MultiWechatCodexService


def write_fake_codex(directory):
    binary = Path(directory) / "fake-codex"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, sys\n"
        "auth = pathlib.Path(os.environ['CODEX_HOME']) / 'auth.json'\n"
        "if sys.argv[1:] == ['logout']:\n"
        "    if auth.exists():\n"
        "        auth.unlink()\n"
        "    print('Successfully logged out', file=sys.stderr)\n"
        "elif sys.argv[1:] == ['login', 'status']:\n"
        "    print('Logged in using ChatGPT' if auth.exists() else 'Not logged in')\n"
        "    sys.exit(0 if auth.exists() else 1)\n"
        "else:\n"
        "    sys.exit(9)\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    return str(binary)


class CodexLogoutManagerTests(unittest.TestCase):
    def setUp(self):
        resolver = patch("wechat_codex_multi.codex_device_login.resolve_codex_bin", side_effect=lambda value: value)
        resolver.start()
        self.addCleanup(resolver.stop)

    def test_logout_removes_only_target_credentials_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main"
            backup = Path(tmp) / "backup"
            for home in [main, backup]:
                home.mkdir()
                (home / "auth.json").write_text("{}")
                (home / "config.toml").write_text('model = "example"')
                (home / "sessions").mkdir()
                (home / "sessions" / "history.jsonl").write_text("history")
            binary = write_fake_codex(tmp)
            manager = CodexDeviceLoginManager(binary)
            refreshed = Mock()
            with patch.dict(os.environ, {"CODEX_HOME": str(backup)}):
                success, message = manager.logout(str(main), on_logged_out=refreshed)
            self.assertTrue(success, message)
            refreshed.assert_called_once_with()
            self.assertFalse(login_status(binary, str(main))[0])
            self.assertTrue(login_status(binary, str(backup))[0])
            for home in [main, backup]:
                self.assertEqual((home / "config.toml").read_text(), 'model = "example"')
                self.assertEqual((home / "sessions" / "history.jsonl").read_text(), "history")
            self.assertTrue(manager.logout(str(main))[0])

    def test_failure_timeout_and_missing_binary_report_failure_and_release_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = CodexDeviceLoginManager(str(Path(tmp) / "fake-codex"))
            failures = [
                (subprocess.CompletedProcess([], 7, "", "logout failed"), "logout failed"),
                (subprocess.TimeoutExpired(["fake-codex", "logout"], 10), "超时"),
                (FileNotFoundError("missing binary"), "missing binary"),
            ]
            for failure, expected in failures:
                with self.subTest(expected=expected):
                    refreshed = Mock()
                    kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                    with patch("wechat_codex_multi.codex_device_login.subprocess.run", **kwargs):
                        success, message = manager.logout(tmp, on_logged_out=refreshed)
                    self.assertFalse(success)
                    self.assertIn(expected, message)
                    refreshed.assert_not_called()
                    with manager.account_run(tmp):
                        pass
                    with patch("wechat_codex_multi.codex_device_login.subprocess.run",
                               return_value=subprocess.CompletedProcess([], 0, "", "")):
                        self.assertTrue(manager.logout(tmp)[0])

    def test_running_task_blocks_logout_for_same_home_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = str(Path(tmp) / "main")
            backup = str(Path(tmp) / "backup")
            manager = CodexDeviceLoginManager("fake-codex")
            with patch("wechat_codex_multi.codex_device_login.subprocess.run",
                       return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                with manager.account_run(main):
                    with manager.account_run(main):
                        pass
                    self.assertFalse(manager.logout(main)[0])
                    run.assert_not_called()
                    self.assertTrue(manager.logout(backup)[0])
                self.assertTrue(manager.logout(main)[0])

    def test_device_login_blocks_logout_without_starting_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = CodexDeviceLoginManager("fake-codex")
            manager._processes[str(Path(tmp).resolve())] = Mock()
            with patch("wechat_codex_multi.codex_device_login.subprocess.run") as run:
                success, message = manager.logout(tmp)
            self.assertFalse(success)
            self.assertIn("设备码登录", message)
            run.assert_not_called()

    def test_logout_excludes_new_tasks_logins_and_duplicate_logout_until_cache_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = CodexDeviceLoginManager("fake-codex")
            entered = threading.Event()
            release = threading.Event()
            results = []

            def run(*_args, **_kwargs):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test logout was not released")
                return subprocess.CompletedProcess([], 0, "", "")

            def refresh():
                with self.assertRaisesRegex(RuntimeError, "正在退出登录"):
                    with manager.account_run(tmp):
                        pass

            with patch("wechat_codex_multi.codex_device_login.subprocess.run", side_effect=run) as cli, \
                    patch("wechat_codex_multi.codex_device_login.subprocess.Popen") as login:
                worker = threading.Thread(target=lambda: results.append(manager.logout(tmp, on_logged_out=refresh)))
                worker.start()
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertFalse(manager.logout(tmp)[0])
                    self.assertFalse(manager.start(tmp, lambda *_: None, lambda *_: None))
                    login.assert_not_called()
                    with self.assertRaisesRegex(RuntimeError, "正在退出登录"):
                        with manager.account_run(tmp):
                            pass
                    with manager.account_run(str(Path(tmp) / "backup")):
                        pass
                    cli.assert_called_once()
                finally:
                    release.set()
                    worker.join(5)
                self.assertFalse(worker.is_alive())
            self.assertEqual(results, [(True, "已退出登录。")])
            with manager.account_run(tmp):
                pass


class CodexLogoutCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["stateDir"] = str(Path(self.tmp.name) / "state")
        config["state"]["saveDebounceMs"] = 0
        config["adminUsers"] = ["admin"]
        config["codex"]["workingDirectory"] = self.tmp.name
        config["codex"]["bin"] = write_fake_codex(self.tmp.name)
        config["codex"]["runner"] = "app-server"
        config["codex"]["accounts"] = []
        for name in ["main", "backup"]:
            home = Path(self.tmp.name) / name
            home.mkdir()
            (home / "auth.json").write_text("{}")
            config["codex"]["accounts"].append({"name": name, "codexHome": str(home)})
        self.service = MultiWechatCodexService(config)
        self.addCleanup(self.service.stop)
        self.service._send_text = Mock()
        self.service._run_codex_and_reply = Mock()
        self.account = {"accountId": "bot"}
        self.key = "bot:admin"

    def send(self, command, user="admin"):
        self.service._handle_message(self.account, user, self.key, command)
        return self.service._send_text.call_args.args[2]

    def test_only_admins_can_logout_and_invalid_commands_never_run_agent(self):
        with patch.object(self.service.codex_device_login, "logout") as logout:
            self.assertIn("只有 adminUsers", self.send("/codex-logout main", user="visitor"))
            for command in ["/codex-logout", "/codex-logout main extra", "/codex-logout main ; echo bad"]:
                self.assertIn("用法", self.send(command))
            for selector in ["unknown", "ma", "1", self.tmp.name]:
                self.assertIn("未知 Codex 账号", self.send(f"/codex-logout {selector}"))
            logout.assert_not_called()
        self.service._run_codex_and_reply.assert_not_called()
        self.assertTrue((Path(self.tmp.name) / "main" / "auth.json").exists())
        self.assertFalse(self.service.stop_event.is_set())

    def test_logout_blocks_queued_tasks_before_any_agent_process_exists(self):
        self.service.tasks.create(self.key, self.key, self.service._task_session(self.key), "等待执行")
        with patch.object(self.service.codex_device_login, "logout") as logout:
            message = self.send("/codex-logout main")
        self.assertIn("排队中", message)
        logout.assert_not_called()
        self.assertTrue((Path(self.tmp.name) / "main" / "auth.json").exists())

    def test_logout_uses_explicit_home_clears_only_its_servers_and_preserves_selection(self):
        self.service.state.update_session(self.key, codexAccount="backup", codexThreadId="saved-thread")
        target = self.service.config["codex"]["accounts"][0]
        backup = self.service.config["codex"]["accounts"][1]
        servers = []
        for agent in ["codex", "desktop"]:
            runner = self.service.codex.runner_for(agent)
            main_server, backup_server, running_backup = Mock(), Mock(), Mock()
            runner.servers.update({target["codexHome"]: main_server, backup["codexHome"]: backup_server})
            runner.run_servers["backup-task"] = running_backup
            servers.append((runner, main_server, backup_server, running_backup))
        self.service._model_options = ["old-model"]
        self.service.desktop_model_options_cache[self.key] = ["old-model"]
        message = self.send("/codex-logout\tmain")
        self.assertIn("Codex 账号 main：已退出登录", message)
        self.assertIn("/codex-login main", message)
        self.assertFalse((Path(target["codexHome"]) / "auth.json").exists())
        self.assertTrue((Path(backup["codexHome"]) / "auth.json").exists())
        for runner, main_server, backup_server, running_backup in servers:
            main_server.close.assert_called_once_with()
            backup_server.close.assert_not_called()
            running_backup.close.assert_not_called()
            self.assertNotIn(target["codexHome"], runner.servers)
            self.assertIs(runner.servers[backup["codexHome"]], backup_server)
            self.assertIs(runner.run_servers["backup-task"], running_backup)
        self.assertIsNone(self.service._model_options)
        self.assertEqual(self.service.desktop_model_options_cache, {})
        session = self.service._get_session(self.key)
        self.assertEqual(session["codexAccount"], "backup")
        self.assertEqual(session["codexThreadId"], "saved-thread")
        self.service._run_codex_and_reply.assert_not_called()
        self.assertFalse(self.service.stop_event.is_set())

    def test_active_task_in_another_workspace_or_same_home_alias_blocks_logout(self):
        other_key = "bot:other:workspace"
        main = self.service.config["codex"]["accounts"][0]
        self.service.config["codex"]["accounts"].append({"name": "alias", "codexHome": main["codexHome"]})
        self.service.state.update_session(other_key, codexAccount="alias", codexClient="desktop")
        with patch.object(self.service, "_active_runs", return_value=[{"agent": "codex", "conversationKey": other_key}]), \
                patch.object(self.service.codex_device_login, "logout") as logout:
            self.assertIn("当前有 Codex 任务运行", self.send("/codex-logout main"))
            logout.assert_not_called()
        self.assertTrue((Path(main["codexHome"]) / "auth.json").exists())

    def test_other_account_task_does_not_block_logout(self):
        other_key = "bot:other"
        self.service.state.update_session(other_key, codexAccount="backup")
        with patch.object(self.service, "_active_runs", return_value=[{"agent": "codex", "conversationKey": other_key}]):
            self.assertIn("已退出登录", self.send("/codex-logout main"))

    def test_active_device_login_blocks_logout_with_cancel_hint(self):
        with patch.object(self.service.codex_device_login, "is_running", return_value=True), \
                patch.object(self.service.codex_device_login, "logout") as logout:
            self.assertIn("/codex-login cancel main", self.send("/codex-logout main"))
            logout.assert_not_called()

    def test_logout_failure_is_reported_without_success_hint_or_cache_refresh(self):
        with patch.object(self.service.codex_device_login, "logout", return_value=(False, "Codex 退出登录失败：test")), \
                patch.object(self.service, "_refresh_codex_account") as refresh:
            message = self.send("/codex-logout main")
        self.assertIn("退出登录失败", message)
        self.assertNotIn("重新登录", message)
        refresh.assert_not_called()

    def test_login_completion_refreshes_the_selected_account(self):
        with patch.object(self.service.codex_device_login, "start", return_value=True) as start, \
                patch.object(self.service, "_refresh_codex_account") as refresh:
            self.send("/codex-login main new@example.com")
            start.call_args.args[2]("Codex 登录成功。")
            refresh.assert_called_once_with(self.service.config["codex"]["accounts"][0])

    def test_task_start_reserves_home_before_runner_process_is_registered(self):
        target = self.service.config["codex"]["accounts"][0]

        def run(*_args):
            with patch("wechat_codex_multi.codex_device_login.subprocess.run") as cli:
                success, message = self.service.codex_device_login.logout(target["codexHome"])
                self.assertFalse(success)
                self.assertIn("任务运行", message)
                cli.assert_not_called()

        with patch.object(self.service, "_run_codex_and_reply_locked", side_effect=run):
            MultiWechatCodexService._run_codex_and_reply(self.service, self.account, "admin", self.key, "task")

    def test_logout_command_bypasses_busy_conversation_and_is_in_help(self):
        lock = self.service._conversation_lock(self.key)
        lock.acquire()
        try:
            self.service._handle_message_safe(self.account, "admin", self.key, "/codex-logout main")
        finally:
            lock.release()
        self.assertIn("已退出登录", self.service._send_text.call_args.args[2])
        self.assertIn("/codex-logout <账号名>", self.service._help_text("bot", full=True))


if __name__ == "__main__":
    unittest.main()
