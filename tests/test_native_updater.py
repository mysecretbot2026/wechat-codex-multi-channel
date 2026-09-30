import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_codex_update import write_bundle
from wechat_codex_multi.cli import main
from wechat_codex_multi.codex_update import CodexUpdateManager, DESKTOP_BUNDLE_ID, make_update_plan
from wechat_codex_multi.native_updater import trigger_builtin_update


class FakeAccessibility:
    def __init__(self, title="Check for Updates…", trusted=True, enabled=True):
        self.item = {"title": title, "enabled": enabled, "children": []}
        self.menu = {"title": "ChatGPT", "children": [
            {"title": "", "children": [{"title": "Quit ChatGPT", "children": []}, self.item]},
        ]}
        self.bar = {"children": [self.menu]}
        self.is_trusted = trusted
        self.pressed = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def trusted(self):
        return self.is_trusted

    def application(self, pid):
        self.pid = pid
        return "app"

    def attribute(self, element, name):
        return self.bar

    def children(self, element):
        return element.get("children", [])

    def title(self, element):
        return element.get("title", "")

    def enabled(self, element):
        return element.get("enabled", False)

    def press(self, element):
        self.pressed.append(element)


class NativeMenuTests(unittest.TestCase):
    def trigger(self, app, api, titles=None, running=True):
        stdout = f"123 {app}/Contents/MacOS/Codex\n" if running else ""
        with patch("wechat_codex_multi.native_updater.sys.platform", "darwin"), \
                patch("wechat_codex_multi.native_updater.subprocess.run",
                      return_value=subprocess.CompletedProcess([], 0, stdout=stdout)), \
                patch("wechat_codex_multi.native_updater.MacAccessibility", return_value=api):
            return trigger_builtin_update(app, DESKTOP_BUNDLE_ID, titles)

    def test_english_and_chinese_update_menu_without_install_or_quit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "ChatGPT.app"
            write_bundle(app)
            for title in ["Check for Updates…", "Check for Updates...", "检查更新…", "檢查更新…"]:
                with self.subTest(title=title):
                    api = FakeAccessibility(title=title)
                    self.assertEqual(self.trigger(app, api), title)
                    self.assertEqual(api.pid, 123)
                    self.assertEqual(api.pressed, [api.menu, api.item])

    def test_accessibility_permission_error_is_actionable_and_does_not_click(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "ChatGPT.app"
            write_bundle(app)
            api = FakeAccessibility(trusted=False)
            with self.assertRaisesRegex(RuntimeError, "辅助功能权限") as error:
                self.trigger(app, api)
            self.assertIn(sys.executable, str(error.exception))
            self.assertEqual(api.pressed, [])

    def test_disabled_and_missing_menu_fail_without_clicking(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "ChatGPT.app"
            write_bundle(app)
            for api, message in [(FakeAccessibility(enabled=False), "不可用"),
                                 (FakeAccessibility(title="Install and Restart"), "未找到")]:
                with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, message):
                    self.trigger(app, api)
                self.assertEqual(api.pressed, [])

    def test_localized_menu_can_be_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "ChatGPT.app"
            write_bundle(app)
            api = FakeAccessibility(title="Rechercher des mises à jour…")
            self.trigger(app, api, ["Rechercher des mises à jour"])
            self.assertEqual(api.pressed, [api.menu, api.item])
            with self.assertRaisesRegex(ValueError, "数组"):
                self.trigger(app, api, "not-an-array")

    def test_closed_app_and_other_bundle_are_not_activated(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "ChatGPT.app"
            write_bundle(app)
            api = FakeAccessibility()
            with self.assertRaisesRegex(RuntimeError, "未运行"):
                self.trigger(app, api, running=False)
            write_bundle(app, bundle_id="com.openai.chat")
            with self.assertRaisesRegex(RuntimeError, "不匹配"):
                self.trigger(app, api)
            self.assertEqual(api.pressed, [])


class NativeUpdateIntegrationTests(unittest.TestCase):
    def config(self, tmp, app):
        return {"stateDir": tmp, "updates": {"desktopAppPath": str(app)}}

    def test_plain_desktop_update_defaults_to_native_and_installer_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "ChatGPT.app"
            write_bundle(app)
            with patch("wechat_codex_multi.codex_update.sys.platform", "darwin"), \
                    patch("wechat_codex_multi.codex_update._desktop_is_running", return_value=False), \
                    patch("wechat_codex_multi.codex_update.shutil.which", return_value=None):
                plan = make_update_plan(self.config(tmp, app), "desktop")
                self.assertEqual(plan.method, "native")
                self.assertEqual(make_update_plan(self.config(tmp, app), "desktop", "installer").method, "dmg")

    def test_native_request_runs_during_agent_task_and_does_not_claim_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "ChatGPT.app"
            write_bundle(app)
            manager = CodexUpdateManager(self.config(tmp, app))
            with patch("wechat_codex_multi.codex_update.sys.platform", "darwin"), \
                    patch("wechat_codex_multi.codex_update.trigger_builtin_update", return_value="检查更新…") as trigger, \
                    patch("wechat_codex_multi.codex_update._run_logged") as installer:
                with manager.agent_run():
                    result = manager.run("desktop")
                trigger.assert_called_once()
                installer.assert_not_called()
                self.assertEqual(result["status"], "requested")
                self.assertNotIn("after", result)
                self.assertIn("尚未确认安装完成", manager.status())
                write_bundle(app, build="2")
                self.assertIn("更新完成", manager.status("desktop"))
                saved = json.loads((manager.directory / "desktop.json").read_text())
                self.assertEqual(saved["status"], "success")
                self.assertEqual(saved["after"], "1.2 (2)")

    def test_native_permission_failure_does_not_fall_back_to_installer(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "ChatGPT.app"
            write_bundle(app)
            manager = CodexUpdateManager(self.config(tmp, app))
            with patch("wechat_codex_multi.codex_update.sys.platform", "darwin"), \
                    patch("wechat_codex_multi.codex_update.trigger_builtin_update", side_effect=RuntimeError("辅助功能权限")), \
                    patch("wechat_codex_multi.codex_update._run_logged") as installer:
                result = manager.run("desktop", desktop_method="native")
            self.assertEqual(result["status"], "failed")
            self.assertIn("辅助功能权限", result["error"])
            installer.assert_not_called()

    def test_menu_request_does_not_refresh_or_close_app_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "ChatGPT.app"
            write_bundle(app)
            manager = CodexUpdateManager(self.config(tmp, app))
            refresh, completed = Mock(), Mock()
            with patch("wechat_codex_multi.codex_update.sys.platform", "darwin"), \
                    patch("wechat_codex_multi.codex_update.trigger_builtin_update", return_value="检查更新…"):
                thread = manager.start("desktop", Mock(), completed, on_updated=refresh, desktop_method="native")
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(completed.call_args.args[0]["status"], "requested")
            refresh.assert_not_called()

    def test_explicit_native_overrides_custom_installer_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "ChatGPT.app"
            write_bundle(app)
            config = self.config(tmp, app)
            config["updates"]["desktopCommand"] = ["my-installer"]
            with patch("wechat_codex_multi.codex_update.sys.platform", "darwin"):
                self.assertEqual(make_update_plan(config, "desktop", "native").method, "native")

    def test_terminal_native_request_returns_zero_but_not_installed_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"stateDir": tmp}))
            with patch("wechat_codex_multi.cli.CodexUpdateManager") as manager, patch("builtins.print") as output:
                manager.return_value.run.return_value = {"target": "desktop", "status": "requested", "method": "native"}
                main(["--config", str(config), "update", "desktop"])
                manager.return_value.run.assert_called_once_with("desktop")
                self.assertIn("尚未确认安装完成", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
