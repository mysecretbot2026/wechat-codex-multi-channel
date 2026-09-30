import io
import json
import os
import plistlib
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wechat_codex_multi.cli import main
from wechat_codex_multi.codex_update import (
    CodexUpdateManager, DESKTOP_BUNDLE_ID, OPENAI_TEAM_ID, UpdatePlan,
    _replace_bundle, _run_logged, _update_dmg, desktop_app_path,
    format_update_result, make_update_plan, parse_update_command,
)
from wechat_codex_multi.service import MultiWechatCodexService


def write_bundle(path, build="1", bundle_id=DESKTOP_BUNDLE_ID):
    info = path / "Contents/Info.plist"
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_bytes(plistlib.dumps({
        "CFBundleIdentifier": bundle_id,
        "CFBundleShortVersionString": f"1.{build}",
        "CFBundleVersion": build,
        "CFBundleExecutable": "Codex",
    }))


class UpdateCommandTests(unittest.TestCase):
    def test_exact_commands_and_aliases(self):
        for command, expected in {
            "/update": ("", "help"),
            "/update cli": ("cli", "run"),
            "/update desktop check": ("desktop", "check"),
            "/update status": ("", "status"),
            "/codex-update status": ("cli", "status"),
            "/d-update": ("desktop", "run"),
            "/d-update check": ("desktop", "check"),
            "/update desktop native": ("desktop", "native"),
            "/d-update installer": ("desktop", "installer"),
        }.items():
            self.assertEqual(parse_update_command(command), expected)
        for command in ["/update cli ; reboot", "/update cli latest", "/update all", "/d-update check extra", "/update cli native"]:
            with self.assertRaises(ValueError):
                parse_update_command(command)
        self.assertIsNone(parse_update_command("/update-config"))

    def test_wechat_commands_require_admin_and_never_run_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MultiWechatCodexService({
                "stateDir": tmp, "codex": {"bin": "codex", "workingDirectory": tmp},
                "adminUsers": ["admin"],
            })
            service._send_text = Mock()
            service._run_codex_and_reply = Mock()
            service.updates.start = Mock()
            service.updates.status = Mock(return_value="状态")
            account = {"accountId": "bot"}
            try:
                for text in ["/update cli", "/d-update", "/update status"]:
                    service._handle_message(account, "visitor", "bot:visitor", text)
                service.updates.start.assert_not_called()
                service.updates.status.assert_not_called()
                with patch("wechat_codex_multi.service.make_update_plan") as planner:
                    planner.return_value.describe.return_value = "只读预览"
                    service._handle_message(account, "admin", "bot:admin", "/codex-update check")
                    planner.assert_called_once_with(service.config, "cli")
                service._handle_message(account, "admin", "bot:admin", "/d-update")
                self.assertEqual(service.updates.start.call_args.args[0], "desktop")
                service._handle_message(account, "admin", "bot:admin", "/update status")
                service.updates.status.assert_called_once_with("")
                service._handle_message(account, "admin", "bot:admin", "/update cli ; touch /tmp/foo")
                self.assertEqual(service.updates.start.call_count, 1)
                service._run_codex_and_reply.assert_not_called()
                for command in ["/update cli", "/codex-update", "/d-update check"]:
                    self.assertTrue(service._can_run_without_conversation_lock(command))
            finally:
                service.stop()

    def test_terminal_update_reports_failure_with_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"stateDir": tmp}))
            with patch("wechat_codex_multi.cli.CodexUpdateManager") as manager, patch("builtins.print"):
                manager.return_value.run.return_value = {"target": "cli", "status": "failed", "error": "失败"}
                with self.assertRaises(SystemExit) as error:
                    main(["--config", str(config), "update", "cli"])
                self.assertEqual(error.exception.code, 1)
                manager.return_value.run.assert_called_once_with("cli")
                with patch("wechat_codex_multi.cli.make_update_plan") as planner:
                    main(["--config", str(config), "update", "desktop", "--check"])
                    planner.assert_called_once()
                self.assertEqual(manager.return_value.run.call_count, 1)


class UpdateDetectionTests(unittest.TestCase):
    def test_npm_updates_the_configured_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "global modules/@openai/codex/bin"
            package.mkdir(parents=True)
            target = package / "codex.js"
            target.touch()
            executable = Path(tmp) / "codex"
            executable.symlink_to(target)
            def which(name):
                return str(executable) if name == "codex" else f"/bin/{name}" if name == "npm" else None
            with patch("wechat_codex_multi.codex_update.shutil.which", side_effect=which), \
                    patch("wechat_codex_multi.codex_update._capture", side_effect=["codex-cli 1", str(package.parents[2])]):
                plan = make_update_plan({"codex": {"bin": "codex"}}, "cli")
            self.assertEqual(plan.method, "npm")
            self.assertEqual(plan.commands, [["/bin/npm", "install", "-g", "@openai/codex@latest"]])

    def test_unknown_install_does_not_install_another_cli(self):
        def which(name):
            return {"custom-codex": "/standalone/codex", "npm": "/bin/npm", "brew": "/bin/brew"}.get(name)
        with patch("wechat_codex_multi.codex_update.shutil.which", side_effect=which), \
                patch("wechat_codex_multi.codex_update._capture", side_effect=["codex-cli 1", "/different/npm/root"]):
            with self.assertRaisesRegex(RuntimeError, "安装来源"):
                make_update_plan({"codex": {"bin": "custom-codex"}}, "cli")

    def test_homebrew_cask_and_legacy_formula(self):
        for directory, kind in [("Caskroom", "--cask"), ("Cellar", "--formula")]:
            with self.subTest(directory=directory):
                def which(name):
                    return f"/generic/{directory}/codex/1/codex" if name == "codex" else "/bin/brew" if name == "brew" else None
                with patch("wechat_codex_multi.codex_update.shutil.which", side_effect=which), \
                        patch("wechat_codex_multi.codex_update._capture", side_effect=["codex-cli 1", "codex 1"]):
                    plan = make_update_plan({}, "cli")
                self.assertEqual(plan.commands[-1], ["/bin/brew", "upgrade", kind, "codex"])

    def test_custom_command_is_argv_and_not_shell_text(self):
        with patch("wechat_codex_multi.codex_update.shutil.which", return_value="/bin/codex"), \
                patch("wechat_codex_multi.codex_update._capture", return_value="codex-cli 1"):
            with self.assertRaisesRegex(ValueError, "argv"):
                make_update_plan({"updates": {"cliCommand": "npm install -g @openai/codex"}}, "cli")
            plan = make_update_plan({"updates": {"cliCommand": ["installer", "literal;argument"]}}, "cli")
        self.assertEqual(plan.commands, [["installer", "literal;argument"]])

    def test_desktop_accepts_renamed_bundle_and_rejects_unrelated_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "ChatGPT.app"
            write_bundle(app)
            config = {"updates": {"desktopAppPath": str(app)}}
            self.assertEqual(desktop_app_path(config), app.resolve())
            (app / "Contents/Info.plist").unlink()
            write_bundle(app, bundle_id="com.openai.chat")
            with self.assertRaisesRegex(RuntimeError, "不是桌面"):
                desktop_app_path(config)

    def test_desktop_custom_commands_work_on_other_platforms(self):
        for platform in ["linux", "win32"]:
            with self.subTest(platform=platform), patch("wechat_codex_multi.codex_update.sys.platform", platform):
                with self.assertRaisesRegex(RuntimeError, "其他系统"):
                    make_update_plan({}, "desktop")
                with patch("wechat_codex_multi.codex_update._capture", return_value="desktop 1"):
                    plan = make_update_plan({"updates": {
                        "desktopCommand": ["deployment-tool", "update"],
                        "desktopVersionCommand": ["version-tool"],
                    }}, "desktop")
                self.assertEqual(plan.before, "desktop 1")
                self.assertEqual(plan.method, "custom")

    def test_desktop_homebrew_owner_and_unrelated_chatgpt_cask(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "ChatGPT.app"
            write_bundle(app)
            for cask, url, expected in [
                ("codex-app", "https://persistent.oaistatic.com/codex-app-prod/ChatGPT.dmg", "homebrew --cask"),
                ("chatgpt", "https://persistent.oaistatic.com/codex-app-prod/ChatGPT.dmg", "homebrew --cask"),
                ("chatgpt", "https://example.com/other-chatgpt.dmg", "dmg"),
            ]:
                def capture(argv, **kwargs):
                    if "list" in argv:
                        return "installed" if argv[-1] == cask else ""
                    return json.dumps({"casks": [{"installed": "1", "url": url, "appdir": tmp,
                                                   "artifacts": [{"app": ["ChatGPT.app"]}]}]})
                with self.subTest(cask=cask, url=url), \
                        patch("wechat_codex_multi.codex_update.sys.platform", "darwin"), \
                        patch("wechat_codex_multi.codex_update.shutil.which", return_value="/bin/brew"), \
                        patch("wechat_codex_multi.codex_update._capture", side_effect=capture):
                    plan = make_update_plan({"updates": {"desktopAppPath": str(app)}}, "desktop", "installer")
                    self.assertEqual(plan.method, expected)


class UpdateExecutionTests(unittest.TestCase):
    def make_plan(self, version_file, code):
        return UpdatePlan("cli", "custom", str(version_file), "1",
                          commands=[[sys.executable, "-c", code]],
                          version_command=[sys.executable, "-c", f"print(open({str(version_file)!r}).read())"])

    def test_actual_subprocess_success_failure_logs_and_persistent_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            version = Path(tmp) / "version"
            version.write_text("1")
            manager = CodexUpdateManager({"stateDir": tmp})
            plan = self.make_plan(version, f"open({str(version)!r},'w').write('2')")
            with patch("wechat_codex_multi.codex_update.make_update_plan", return_value=plan):
                result = manager.run("cli")
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["after"], "2")
            self.assertIn("更新完成", Path(result["log"]).read_text())
            self.assertIn("更新后：2", CodexUpdateManager({"stateDir": tmp}).status())
            plan.commands = [[sys.executable, "-c", "print('install failed'); raise SystemExit(7)"]]
            with patch("wechat_codex_multi.codex_update.make_update_plan", return_value=plan):
                result = manager.run("cli")
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("after", result)
            self.assertIn("install failed", Path(result["log"]).read_text())

    def test_version_probe_failure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = UpdatePlan("cli", "custom", "/fake", "1", commands=[],
                              version_command=[sys.executable, "-c", "raise SystemExit(2)"])
            with patch("wechat_codex_multi.codex_update.make_update_plan", return_value=plan):
                result = CodexUpdateManager({"stateDir": tmp}).run("cli")
            self.assertEqual(result["status"], "failed")

    def test_timeout_is_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = UpdatePlan("cli", "custom", "/fake", "1",
                              commands=[[sys.executable, "-c", "import time; time.sleep(20)"]])
            with patch("wechat_codex_multi.codex_update.make_update_plan", return_value=plan):
                result = CodexUpdateManager({"stateDir": tmp, "updates": {"timeoutSeconds": 1}}).run("cli")
            self.assertEqual(result["status"], "failed")
            self.assertIn("超时", result["error"])

    def test_shared_agent_locks_and_exclusive_update_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = CodexUpdateManager({"stateDir": tmp}), CodexUpdateManager({"stateDir": tmp})
            with a.agent_run(), b.agent_run():
                with self.assertRaisesRegex(RuntimeError, "正在运行"):
                    a.run("cli")
            handle = a._lock()
            try:
                with self.assertRaises(RuntimeError):
                    b.run("desktop", desktop_method="installer")
                with self.assertRaises(RuntimeError):
                    with b.agent_run():
                        self.fail("must not admit an agent during update")
            finally:
                handle.close()
            with b.agent_run():
                pass

    def test_background_notification_order_and_start_is_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = CodexUpdateManager({"stateDir": tmp})
            events, entered, release = [], threading.Event(), threading.Event()
            def execute(record):
                entered.set()
                release.wait(3)
                return dict(record, status="success")
            with patch.object(manager, "_execute", side_effect=execute):
                thread = manager.start("cli", lambda text: events.append(text),
                                       lambda record: events.append(record["status"]),
                                       on_updated=lambda: events.append("refresh"))
                self.assertTrue(entered.wait(3))
                self.assertIn("正在更新", events[0])
                self.assertIn("正在更新", manager.status())
                with self.assertRaises(RuntimeError):
                    manager.run("desktop", desktop_method="installer")
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(events[1:], ["refresh", "success"])

    def test_interrupted_update_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = CodexUpdateManager({"stateDir": tmp})
            handle, record = manager._prepare("cli")
            self.assertIn("正在更新", manager.status())
            handle.close()
            with manager.agent_run():
                self.assertIn("未记录成功", CodexUpdateManager({"stateDir": tmp}).status("cli"))

    @unittest.skipIf(os.name == "nt", "Unix file descriptor inheritance")
    def test_package_manager_inherits_update_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = CodexUpdateManager({"stateDir": tmp})
            handle = manager._lock()
            process = Mock()
            process.wait.return_value = 0
            try:
                with manager._inherit_lock(handle), \
                        patch("wechat_codex_multi.codex_update.subprocess.Popen", return_value=process) as spawn:
                    _run_logged(["installer"], io.StringIO(), 20)
                    self.assertEqual(spawn.call_args.kwargs["pass_fds"], (handle.fileno(),))
            finally:
                handle.close()

    def test_unchanged_version_is_reported_honestly(self):
        value = format_update_result({"target": "cli", "status": "success", "before": "1", "after": "1"})
        self.assertIn("版本未变化", value)


class DesktopInstallTests(unittest.TestCase):
    def run_dmg(self, app, new_build="2", signature_failure=False, running=None):
        calls = []
        def run(argv, output, timeout):
            calls.append(argv)
            if argv[0].endswith("hdiutil") and argv[1] == "attach":
                mount = Path(argv[argv.index("-mountpoint") + 1])
                write_bundle(mount / "ChatGPT.app", build=new_build)
            if argv[0].endswith("codesign") and signature_failure:
                raise RuntimeError("signature rejected")
            if argv[0].endswith("ditto"):
                shutil.copytree(argv[1], argv[2])
        plan = UpdatePlan("desktop", "dmg", str(app), "1.1 (1)", app_path=str(app))
        with patch("wechat_codex_multi.codex_update._run_logged", side_effect=run), \
                patch("wechat_codex_multi.codex_update._desktop_is_running", side_effect=running or [False, False]):
            _update_dmg(plan, io.StringIO(), 20)
        return calls

    def test_dmg_validates_official_signer_before_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "Codex.app"
            write_bundle(app)
            calls = self.run_dmg(app)
            self.assertEqual(plistlib.loads((app / "Contents/Info.plist").read_bytes())["CFBundleVersion"], "2")
            self.assertTrue(any(OPENAI_TEAM_ID in argument for argv in calls for argument in argv))
            self.assertTrue(any("spctl" in argv[0] for argv in calls))
            self.assertEqual(list(Path(tmp).iterdir()), [app])

    def test_signature_rejection_keeps_old_application(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "ChatGPT.app"
            write_bundle(app)
            with self.assertRaisesRegex(RuntimeError, "signature rejected"):
                self.run_dmg(app, signature_failure=True)
            self.assertEqual(plistlib.loads((app / "Contents/Info.plist").read_bytes())["CFBundleVersion"], "1")

    def test_equal_or_older_build_does_not_replace_application(self):
        for build in ["0", "1"]:
            with self.subTest(build=build), tempfile.TemporaryDirectory() as tmp:
                app = Path(tmp) / "ChatGPT.app"
                write_bundle(app)
                calls = self.run_dmg(app, new_build=build)
                self.assertFalse(any("ditto" in argv[0] for argv in calls))

    def test_app_reopened_during_download_aborts_without_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "ChatGPT.app"
            write_bundle(app)
            with self.assertRaisesRegex(RuntimeError, "被打开"):
                self.run_dmg(app, running=[False, True])
            self.assertEqual(plistlib.loads((app / "Contents/Info.plist").read_bytes())["CFBundleVersion"], "1")

    def test_failed_atomic_replacement_restores_old_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, staged, backup = [Path(tmp) / name for name in ["old.app", "new.app", "backup.app"]]
            write_bundle(app)
            write_bundle(staged, build="2")
            original = Path.rename
            def rename(path, destination):
                if path == staged:
                    raise OSError("replacement failed")
                return original(path, destination)
            with patch.object(Path, "rename", rename):
                with self.assertRaisesRegex(OSError, "replacement failed"):
                    _replace_bundle(staged, app, backup)
            self.assertTrue(app.exists())
            self.assertFalse(backup.exists())


if __name__ == "__main__":
    unittest.main()
